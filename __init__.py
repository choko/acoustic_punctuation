import logging
import theano

from collections import Counter
from theano import tensor
from toolz import merge

from blocks.algorithms import (GradientDescent, StepClipping, AdaDelta, CompositeRule)
from blocks.extensions import FinishAfter, Printing
from blocks.extensions.monitoring import TrainingDataMonitoring
from blocks.filter import VariableFilter
from blocks.graph import ComputationGraph, apply_noise, apply_dropout
from blocks.initialization import IsotropicGaussian, Orthogonal, Constant
from blocks.main_loop import MainLoop
from blocks.model import Model
from blocks.select import Selector

from checkpoint import CheckpointNMT, LoadNMT
from model import BidirectionalEncoder, BidirectionalAudioEncoder, Decoder
from sampling import F1Validator, Sampler

try:
    from blocks_extras.extensions.plot import Plot
    BOKEH_AVAILABLE = True
except ImportError:
    BOKEH_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

theano.config.on_unused_input = 'warn'
theano.config.exception_verbosity = 'low'


def main(config, tr_stream, dev_stream, use_bokeh=False):

    # Create Theano variables
    logger.info('Creating theano variables')

    if config["input"] == "words":
        input_words = tensor.lmatrix('words')
        input_words_mask = tensor.matrix('words_mask')
        punctuation_marks = tensor.lmatrix('punctuation_marks')
        punctuation_marks_mask = tensor.matrix('punctuation_marks_mask')

        # Construct model
        logger.info('Building RNN encoder-decoder')
        encoder = BidirectionalEncoder(
            config['src_vocab_size'], config['enc_embed'], config['enc_nhids'])
        decoder = Decoder(
            config['trg_vocab_size'], config['dec_embed'], config['dec_nhids'],
            config['enc_nhids'] * 2)
        cost = decoder.cost(
            encoder.apply(input_words, input_words_mask),
            input_words_mask, punctuation_marks, punctuation_marks_mask)
    elif config["input"] == "audio":
        audio = tensor.ftensor3('audio')
        audio_mask = tensor.matrix('audio_mask')
        words_ends = tensor.lmatrix('words_ends')
        words_ends_mask = tensor.matrix('words_ends_mask')
        punctuation_marks = tensor.lmatrix('punctuation_marks')
        punctuation_marks_mask = tensor.matrix('punctuation_marks_mask')

        # Construct model
        logger.info('Building RNN encoder-decoder')
        encoder = BidirectionalAudioEncoder(
            config['audio_feat_size'], config['enc_embed'], config['enc_nhids'])
        decoder = Decoder(
            config['trg_vocab_size'], config['dec_embed'], config['dec_nhids'],
            config['enc_nhids'] * 2)
        cost = decoder.cost(
            encoder.apply(audio, audio_mask, words_ends, words_ends_mask),
            punctuation_marks_mask, punctuation_marks, punctuation_marks_mask)



    logger.info('Creating computational graph')
    cg = ComputationGraph(cost)

    # Initialize model
    logger.info('Initializing model')
    encoder.weights_init = decoder.weights_init = IsotropicGaussian(config['weight_scale'])
    encoder.biases_init = decoder.biases_init = Constant(0)
    encoder.push_initialization_config()
    decoder.push_initialization_config()
    encoder.bidir.prototype.weights_init = Orthogonal()
    if config["input"] == "audio":
        encoder.embedding.prototype.weights_init = Orthogonal()
    decoder.transition.weights_init = Orthogonal()
    encoder.initialize()
    decoder.initialize()

    # apply dropout for regularization
    if config['dropout'] < 1.0:
        # dropout is applied to the output of maxout in ghog
        logger.info('Applying dropout')
        dropout_inputs = [x for x in cg.intermediary_variables if x.name == 'maxout_apply_output']
        cg = apply_dropout(cg, dropout_inputs, config['dropout'])

    # Apply weight noise for regularization
    if config['weight_noise_ff'] > 0.0:
        logger.info('Applying weight noise to ff layers')
        enc_params = Selector(encoder.lookup).get_params().values()
        enc_params += Selector(encoder.fwd_fork).get_params().values()
        enc_params += Selector(encoder.back_fork).get_params().values()
        dec_params = Selector(decoder.sequence_generator.readout).get_params().values()
        dec_params += Selector(decoder.sequence_generator.fork).get_params().values()
        dec_params += Selector(decoder.state_init).get_params().values()
        cg = apply_noise(cg, enc_params+dec_params, config['weight_noise_ff'])

    # Print shapes
    shapes = [param.get_value().shape for param in cg.parameters]
    logger.info("Parameter shapes: ")
    for shape, count in Counter(shapes).most_common():
        logger.info('    {:15}: {}'.format(shape, count))
    logger.info("Total number of parameters: {}".format(len(shapes)))

    # Print parameter names
    enc_dec_param_dict = merge(Selector(encoder).get_parameters(), Selector(decoder).get_parameters())
    logger.info("Parameter names: ")
    for name, value in enc_dec_param_dict.items():
        logger.info('    {:15}: {}'.format(value.get_value().shape, name))
    logger.info("Total number of parameters: {}".format(len(enc_dec_param_dict)))

    # Set up training model
    logger.info("Building model")
    training_model = Model(cost)

    # Set extensions
    logger.info("Initializing extensions")
    extensions = [
        FinishAfter(after_n_batches=config['finish_after']),
        TrainingDataMonitoring([cost], after_batch=True),
        Printing(after_batch=True),
        CheckpointNMT(config['saveto'], every_n_batches=config['save_freq'])
    ]

    # Set up beam search and sampling computation graphs if necessary
    if config['hook_samples'] >= 1 or config['f1_validation'] is not None:
        logger.info("Building sampling model")
        if config["input"] == "words":
            sampling_input_words = tensor.lmatrix('sampling_words')
            sampling_input_words_mask = tensor.ones((sampling_input_words.shape[0], sampling_input_words.shape[1]))
            sampling_representation = encoder.apply(sampling_input_words, sampling_input_words_mask)
        elif config["input"] == "audio":
            sampling_audio = tensor.ftensor3('sampling_audio')
            sampling_audio_mask = tensor.ones((sampling_audio.shape[0], sampling_audio.shape[1]))
            sampling_words_ends = tensor.lmatrix('sampling_words_ends')
            sampling_words_ends_mask = tensor.ones((sampling_words_ends.shape[0], sampling_words_ends.shape[1]))
            sampling_representation = encoder.apply(sampling_audio, sampling_audio_mask, sampling_words_ends, sampling_words_ends_mask)

        generated = decoder.generate(sampling_representation)
        search_model = Model(generated)
        _, samples = VariableFilter(
            bricks=[decoder.sequence_generator], name="outputs")(
                ComputationGraph(generated[1]))  # generated[1] is next_outputs

    # Add sampling
    if config['hook_samples'] >= 1:
        logger.info("Building sampler")
        extensions.append(
            Sampler(model=search_model, data_stream=tr_stream,
                    src_vocab=config['src_vocab'], trg_vocab=config['trg_vocab'],
                    hook_samples=config['hook_samples'],
                    every_n_batches=config['sampling_freq'],
                    src_vocab_size=config['src_vocab_size']))

    # Add early stopping based on f1
    if config['f1_validation'] is not None:
        logger.info("Building f1 validator")
        extensions.append(
            F1Validator(samples=samples, config=config,
                          model=search_model, data_stream=dev_stream,
                          normalize=config['normalized_f1'],
                          every_n_batches=config['f1_val_freq']))

    # Reload model if necessary
    if config['reload']:
        extensions.append(LoadNMT(config['saveto']))

    # Plot cost in bokeh if necessary
    if use_bokeh and BOKEH_AVAILABLE:
        extensions.append(
            Plot('Cs-En', channels=[['decoder_cost_cost']],
                 after_batch=True))

    # Set up training algorithm
    logger.info("Initializing training algorithm")
    algorithm = GradientDescent(
        cost=cost, parameters=cg.parameters,
        step_rule=CompositeRule([StepClipping(config['step_clipping']), eval(config['step_rule'])()]),
        on_unused_sources='warn'
    )

    # Initialize main loop
    logger.info("Initializing main loop")
    main_loop = MainLoop(
        model=training_model,
        algorithm=algorithm,
        data_stream=tr_stream,
        extensions=extensions
    )

    # Train!
    main_loop.run()
