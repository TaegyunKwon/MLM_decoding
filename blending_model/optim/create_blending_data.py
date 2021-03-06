"""This contains functions that can be used to create data for training a blending model. It should
be run from the command line."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/../..')

import argparse
import dataMaps
import decode
import numpy as np
import pickle
import itertools
import glob
import gzip

import sampling
from beam import Beam
from state import trinary_pr_to_presence_onset
from mlm_training.model import Model, make_model_param

from train_blending_model import filter_data_by_min_diff



def add_noise_to_input_data(input_data, noise, noise_gauss):
    """
    Add controlled noise to the given input_data.
    
    Parameters
    ----------
    input_data : np.ndarray
        (P, T) array of float values, acoustic model activations.
        
    noise : float
        The amount of noise to add to the acoustic pianoroll activations. Unless
        noise_gauss is True, the noise will be uniform on the range (0, noise),
        and added towards the value of 0.5. If None, no noise will be added.
        
    noise_gauss : boolean
        If True, the given noise will be Gaussian, centered around 0 with standard
        deviation given by noise. The value will be added or subtracted to bring the
        value of each activation closer to 0.5. If False, the noise will be uniform
        as described in noise.
        
    Returns
    -------
    input_data : np.ndarray
        (P, T) array of float values, the input acoustic model activations with the
        described noise added.
    """
    if noise is None:
        return input_data
    
    noise_func = np.random.randn if noise_gauss else np.random.rand
    noise = np.abs(noise * noise_func(*input_data.shape))
    input_data = np.where(input_data > 0.5, input_data - noise, input_data + noise)
    return np.clip(input_data, 0.001, 0.999)



def get_weight_data_one_piece(filename, section, model, sess, args):
    """
    Get the x, y, and d data for a single piece.

    Parameters
    ----------
    filename : string
        The MIDI file whose data to return.

    section : list
        A list containing the start and end points of the section of the piece to decode,
        in seconds. If None, the entire piece is decoded.

    model : tf.model
        The tensorflow model to use.

    sess : tf.Session
        The tensorflow session to use.

    args : Namespace
        A namespace returned by argparse which contains the fields we need here.
        Run create_blending_data.py -h for details.
    """
    if args.step == "beat":
        data = dataMaps.DataMapsBeats()
        data.make_from_file(filename, args.beat_gt, args.beat_subdiv, section,
                            with_onsets=args.with_onsets, acoustic_model=args.acoustic)
    else:
        data = dataMaps.DataMaps()
        data.make_from_file(filename, args.step, section=section, with_onsets=args.with_onsets,
                            acoustic_model=args.acoustic)

    input_data = data.input
    target_data = data.target
    if args.with_onsets:
        input_data = np.zeros((data.input.shape[0] * 2, data.input.shape[1]))
        input_data[:data.input.shape[0], :] = data.input[:, :, 0]
        input_data[data.input.shape[0]:, :] = data.input[:, :, 1]
        target_data = trinary_pr_to_presence_onset(data.target)
        
    # Add noise
    input_data = add_noise_to_input_data(input_data, args.noise, args.noise_gauss)

    # Decode
    return get_weight_data(target_data, input_data, model, sess, branch_factor=args.branch, beam_size=args.beam,
                           weight=[[args.weight], [1 - args.weight]], hash_length=args.hash,
                           gt_only=args.gt, history=args.history, features=not args.no_features,
                           min_diff=args.min_diff, verbose=args.verbose)



def get_weight_data(gt, acoustic, model, sess, branch_factor=50, beam_size=200, union=False, weight=[[0.5], [0.5]],
           hash_length=10, gt_only=False, history=5, min_diff=0.01, features=False, verbose=False):
    """
    Get the average ranks of the ground truth frame from decode.enumerate_samples().

    Parameters
    ==========
    gt : matrix
        The ground truth binary piano roll, 88 x T.

    acoustic : matrix
        A probabilistic piano roll, 88 x T, containing values between 0.0 and 1.0
        inclusive. acoustic[p, t] represents the probability of pitch p being present
        at frame t.

    model : Model
        The language model to use for the transduction process.

    sess : tf.session
        The session for the given model.

    branch_factor : int
        The number of samples to use per frame. Defaults to 50.

    beam_size : int
        The beam size for the search. Defaults to 50.

    union : boolean
        True to use union sampling. False (default) to use joint sampling with the weight.

    weight : list
        A length-2 list, whose first element is the weight for the acoustic model and whose 2nd
        element is the weight for the language model. This list should be normalized to sum to 1.
        Defaults to [0.5, 0.5].

    hash_length : int
        The history length for the hashed beam. If two states do not differ in the past hash_length
        frames, only the most probable one is saved in the beam. Defaults to 10.

    gt_only : boolean
        True to transition only on the ground truth sample, no matter its rank. Flase to transition
        normally. Defaults to False.

    history : int
        How many frames to save in the x data point. Defaults to 5.

    min_diff : float
        The minimum difference (between language and acoustic) to save a data point. Defaults to 0.01.

    features : boolean
        Whether to use features in the weight_model's data points. Defaults to False.


    Returns
    =======
    x : np.ndarray
        The x data from this decoding process. A (data x 7) size matrix.

    y : np.ndarray
        The y data from this decoding process. A (data x 2) array.

    diffs : np.array
        The differences between the language and acoustic model priors for each data point.
    """
    weights_all = None
    priors_all = None
    P = len(acoustic)

    beam = Beam()
    beam.add_initial_state(model, sess, P)

    acoustic = np.transpose(acoustic)

    x = np.zeros((0, 0))
    y = np.zeros((0, 0)) if model.with_onsets else np.zeros(0)
    diffs = np.zeros((0, 0)) if model.with_onsets else np.zeros(0)

    gt = np.transpose(gt)

    lstm_transform = None
    if model.with_onsets:
        lstm_transform = decode.three_hot_output_to_presence_onset

    for frame_num, (gt_frame, frame) in enumerate(zip(gt, acoustic)):
        if verbose and frame_num % 20 == 0:
            print(str(frame_num) + " / " + str(acoustic.shape[0]))

        # Run the LSTM!
        if frame_num != 0:
            decode.run_lstm(sess, model, beam, transform=lstm_transform)

        # Here, beam contains a list of states, with sample histories, priors, and LSTM hidden_states,
        # but needs to be updated with weights and combined_priors when sampling.

        # Get data
        for state in beam:
            pitches = np.argwhere(1 - np.isclose(np.squeeze(state.prior), np.squeeze(frame),
                                                 rtol=0.0, atol=min_diff))[:,0] if min_diff > 0 else np.arange(P)
            if model.with_onsets:
                pitches = np.unique(np.where(pitches >= (P // 2), pitches - (P // 2), pitches))

            if len(pitches) > 0:
                if len(x) > 0:
                    x = np.vstack((x, decode.create_weight_x_sk(state, acoustic, frame_num, history, pitches=pitches,
                                                                features=features, with_onsets=model.with_onsets)))
                else:
                    x = decode.create_weight_x_sk(state, acoustic, frame_num, history, pitches=pitches, features=features,
                                                  with_onsets=model.with_onsets)

                if model.with_onsets:
                    gt_presence, gt_onset = np.split(gt_frame, 2)
                    gt_new = np.vstack((gt_presence, gt_onset)).T
                    frame_presence, frame_onset = np.split(frame, 2)
                    frame_new = np.vstack((frame_presence, frame_onset)).T
                    prior_presence, prior_onset = np.split(state.prior, 2)
                    prior_new = np.vstack((prior_presence, prior_onset)).T
                    if len(y) > 0:
                        y = np.vstack((y, gt_new[pitches]))
                        diffs = np.vstack((diffs, np.abs(frame_new[pitches] - prior_new[pitches])))
                    else:
                        y = gt_new[pitches]
                        diffs = np.abs(frame_new[pitches] - prior_new[pitches])
                else:
                    y = np.append(y, gt_frame[pitches])
                    diffs = np.append(diffs, np.abs(np.squeeze(frame)[pitches] - np.squeeze(state.prior)[pitches]))

        new_beam = Beam()

        # Here we sample from each state in the beam
        if gt_only:
            new_beam.add(state.transition(gt_frame, 0.0))

        else:
            for i, state in enumerate(beam):
                weight_this = weights_all[:, i * P : (i + 1) * P] if weights_all is not None else weight

                if priors_all is not None:
                    prior = np.squeeze(priors_all[i * P : (i + 1) * P])
                else:
                    prior = np.squeeze(weight_this[0] * frame + weight_this[1] * state.prior)

                # Update state
                state.update_from_weight_model(weight_this[0], prior)

                for log_prob, sample in itertools.islice(sampling.enumerate_samples(prior), branch_factor):

                    # Format the sample (return from enumerate_samples is an array of indexes)
                    if model.with_onsets:
                        sample = sampling.trinarize_with_onsets(sample, P)
                    else:
                        sample = sampling.binarize(sample, P)

                    # Transition on sample
                    new_beam.add(state.transition(sample, log_prob))

        new_beam.cut_to_size(beam_size, min(hash_length, frame_num + 1))
        beam = new_beam

    return x, y, diffs




if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("MIDI", help="The MIDI file to load, or a directory containing MIDI files. " +
                        "They should be in the same location as the " +
                        "corresponding acoustic csvs, and have the same name (except the extension).")

    parser.add_argument("--out", help="The file to save data.", required=True)

    parser.add_argument("-m", "--model", help="The location of the trained language model.", required=True)
    parser.add_argument("--hidden", help="The number of hidden layers in the language model. Defaults to 256",
                        type=int, default=256)

    parser.add_argument("--step", type=str, choices=["time", "quant","quant_short", "event", "beat", "20ms"], help="Change the step type " +
                        "for frame timing. Either time (default), quant (for 16th notes), or event (for onsets).",
                        default="time")
    parser.add_argument('--beat_gt',action='store_true',help="with beat timesteps, use ground-truth beat positions")
    parser.add_argument('--beat_subdiv',type=str,help="with beat timesteps, beat subdivisions to use (comma separated list, without brackets)",default='0,1/4,1/3,1/2,2/3,3/4')

    parser.add_argument("--acoustic", type=str, choices=["kelz", "bittner"], help="Change the acoustic model " +
                        "used in the files. Either kelz (default), or bittner.",
                        default="kelz")

    parser.add_argument("-b", "--beam", help="The beam size. Defaults to 10.", type=int, default=10)
    parser.add_argument("-k", "--branch", help="The branching factor. Defaults to 5.", type=int, default=5)

    parser.add_argument("-w", "--weight", help="The weight for the acoustic model (between 0 and 1). " +
                        "Defaults to 0.8", type=float, default=0.8)

    parser.add_argument("--max_len",type=str,help="test on the first max_len seconds of each text file. " +
                        "Anything other than a number will evaluate on whole files. Default is 30s.",
                        default=30)

    parser.add_argument("--hash", help="The hash length to use. Defaults to 10.",
                        type=int, default=10)

    parser.add_argument("--history", help="The history length to use.",
                        type=int, default=None)

    parser.add_argument("--min_diff", help="The minimum difference (between language and acoustic) to " +
                        "save a data point.", type=float, default=0.01)

    parser.add_argument("--gt", help="Transition on ground truth samples only.", action="store_true")

    parser.add_argument("--no_features", help="Don't save features in the x data points.", action="store_true")

    parser.add_argument("--gpu", help="The GPU to use. Defaults to 0.", default="0")
    parser.add_argument("--cpu", help="Use the CPU.", action="store_true")

    parser.add_argument("-v", "--verbose", help="Print frame updates", action="store_true")

    parser.add_argument("--diagRNN", help="Use diagonal RNN units", action="store_true")

    parser.add_argument("--with_onsets", help="The input will be a double pianoroll containing "
                        "presence and onset halves.", action="store_true")

    parser.add_argument("--noise", help="The amount of noise to add to the acoustic pianoroll "
                        "activations. Unless --noise_gauss is given, the noise will be uniform on "
                        "the range (0, noise), and added towards the value of 0.5.", type=float,
                        default=None)
    parser.add_argument("--noise_gauss", help="The given noise will be Gaussian, centered around 0 with "
                        "standard deviation given by --noise. The value will be added or subtracted to "
                        "bring the value of each activation closer to 0.5.", action="store_true")

    args = parser.parse_args()

    if not (0 <= args.weight <= 1):
        print("Weight must be between 0 and 1.", file=sys.stderr)
        sys.exit(1)

    if args.history is None:
        if args.step in ["time", "20ms"]:
            args.history = 50
        elif args.step in ["beat"]:
            args.history = 12
        else:
            args.history = 10

    try:
        max_len = float(args.max_len)
        section = [0, max_len]
    except:
        max_len = None
        section = None

    if args.cpu:
        args.gpu = ""
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    # Load model
    model_param = make_model_param()
    model_param['n_hidden'] = args.hidden
    model_param['n_steps'] = 1 # To generate 1 step at a time
    model_param['pitchwise']=False
    if args.diagRNN:
        model_param['cell_type'] = "diagLSTM"
    model_param['with_onsets'] = args.with_onsets

    # Build model object
    model = Model(model_param)
    sess,_ = model.load(args.model, model_path=args.model)

    # Load data
    if args.MIDI.endswith(".mid"):
        X, Y, D = get_weight_data_one_piece(args.MIDI, section, model, sess, args)
    else:
        X = np.zeros((0, 0))
        Y = np.zeros((0, 0)) if args.with_onsets else np.zeros(0)
        D = np.zeros((0, 0)) if args.with_onsets else np.zeros(0)

        for file in sorted(glob.glob(os.path.join(args.MIDI, "*.mid"))):
            if args.verbose:
                print(file)

            x, y, d = get_weight_data_one_piece(file, section, model, sess, args)

            if len(X) > 0:
                X = np.vstack((X, x))
                if args.with_onsets:
                    Y = np.vstack((Y, y))
                    D = np.vstack((D, d))
                else:
                    Y = np.append(Y, y)
                    D = np.append(D, d)
            else:
                X = x
                Y = y
                D = d

    print(X.shape)
    print(Y.shape)
    print(D.shape)

    if os.path.dirname(args.out) != '':
        os.makedirs(os.path.dirname(args.out), exist_ok=True)

    # Save data
    success = False
    while not success:
        try:
            with gzip.open(args.out, "wb") as file:
                pickle.dump({'X' : X,
                             'Y' : Y,
                             'D' : D,
                             'history' : args.history,
                             'features' : not args.no_features,
                             'with_onsets' : args.with_onsets,
                             'noise' : args.noise,
                             'noise_gauss' : args.noise_gauss}, file)
            success = True
        except OverflowError:
            args.min_diff += 0.1
            print(f"Too much data created. Trying again with min_diff {args.min_diff}", file=sys.stderr)
            X, Y, D = filter_data_by_min_diff(X, Y, D, args.min_diff, return_D=True)
