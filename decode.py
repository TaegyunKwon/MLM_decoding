import numpy as np
import itertools
import queue
import argparse
import pretty_midi
import sys

import dataMaps
from beam import Beam
from state import State
from mlm_training.model import Model, make_model_param


def decode(acoustic, model, sess, branch_factor=50, beam_size=200, sampling_method="joint", weight=[0.5, 0.5]):
    """
    Transduce the given acoustic probabilistic piano roll into a binary piano roll.
    
    Parameters
    ==========
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
        
    sampling_method : string
        The method to draw samples at each step. Can be either "joint" (default) or "union".
        
    weight : list
        A length-2 list, whose first element is the weight for the acoustic model and whose 2nd
        element is the weight for the language model. This list should be normalized to sum to 1.
        Defaults to [0.5, 0.5].
        
    
    Returns
    =======
    piano_roll : np.matrix
        An 88 x T binary piano roll, where a 1 represents the presence of a pitch
        at a given frame.
        
    priors : np.matrix
        An 88 x T piano roll, giving the prior assigned to each pitch detection by the
        most probable language model state.
    """
    if sampling_method == "union":
        branch_factor = int(branch_factor / 2)
    
    beam = Beam()
    beam.add_initial_state(model, sess)
    
    for frame in np.transpose(acoustic):
        states = []
        samples = []
        log_probs = []
        
        # Used for union sampling
        unique_samples = []
        
        # Gather all computations to perform them batched
        # Acoustic sampling is done separately because the acoustic samples will be identical for every state.
        if sampling_method is "union" or weight[0] == 1.0:
            # If sampling method is acoustic (or union), we generate the same samples for every current hypothesis
            for _, sample in itertools.islice(enumerate_samples(frame, beam.beam[0].prior, weight=[1.0, 0.0]), branch_factor):
                binary_sample = np.zeros(88)
                binary_sample[sample] = 1
                
                # This is used to check for overlaps in union case
                if sampling_method is "union":
                    unique_samples.append(binary_sample)
                
                for state in beam:
                    states.append(state)
                    samples.append(binary_sample)
                    log_probs.append(get_log_prob(binary_sample, frame, state.prior, weight))
            
        if sampling_method is "union" or weight[0] != 1.0:
            for state in beam:
                for _, sample in itertools.islice(enumerate_samples(frame, state.prior,
                                                  weight=[0.0, 1.0] if sampling_method is "union" else weight),
                                                  branch_factor):
                    binary_sample = np.zeros(88)
                    binary_sample[sample] = 1

                    # Overlap with acoustic sample in union case. Skip this sample.
                    if not (sampling_method is "union" and binary_sample in unique_samples):
                        states.append(state)
                        samples.append(binary_sample)
                        log_probs.append(get_log_prob(binary_sample, frame, state.prior, weight))
                
        np_samples = np.zeros((len(samples), 1, 88))
        for i, sample in enumerate(samples):
            np_samples[i, 0, :] = sample
        
        hidden_states, priors = model.run_one_step([s.hidden_state for s in states], np_samples, sess)
        
        beam = Beam()
        for hidden_state, prior, log_prob, state, sample in zip(hidden_states, priors, log_probs, states, samples):
            beam.add(state.transition(sample, log_prob, hidden_state, prior))
        
        beam.cut_to_size(beam_size)
        
    return beam.get_top_state().get_piano_roll(), beam.get_top_state().get_priors()




def get_log_prob(sample, acoustic, language, weight):
    """
    Get the log probability of the given sample given the priors.
    
    Parameters
    ==========
    sample : vector
        An 88-length binarized vector, containing pitch detections.
    
    acoustic : vector
        An 88-length array, containing the probability of each pitch being present,
        according to the acoustic model.
        
    language : vector
        An 88-length array, containing the probability of each pitch being present,
        according to the language model.
        
    weight : list
        A length-2 list, whose first element is the weight for the acoustic model and whose 2nd
        element is the weight for the language model. This list should be normalized to sum to 1.
        Defaults to [0.5, 0.5].
        
    Returns
    =======
    log_prob : float
        The log probability of the given sample, as a weighted sum of p(. | acoustic) and p(. | language).
    """
    p = np.squeeze(weight[0] * acoustic + weight[1] * language)
    not_p = np.squeeze(weight[0] * (1 - acoustic) + weight[1] * (1 - language))
    
    return np.sum(np.where(sample == 1, np.log(p), np.log(not_p)))




def enumerate_samples(acoustic, language, weight=[0.5, 0.5]):
    """
    Enumerate the binarised piano-roll samples of a frame, ordered by probability.
    
    Based on Algorithm 2 from:
    Boulanger-Lewandowski, Bengio, Vincent - 2013 - High-dimensional Sequence Transduction
    
    Parameters
    ==========
    acoustic : np.ndarray
        An 88-length array, containing the probability of each pitch being present,
        according to the acoustic model.
        
    language : np.ndarray
        An 88-length array, containing the probability of each pitch being present,
        according to the language model.
        
    weight : list
        A length-2 list, whose first element is the weight for the acoustic model and whose 2nd
        element is the weight for the language model. This list should be normalized to sum to 1.
        Defaults to [0.5, 0.5].
        
    Return
    ======
    A generator for the given samples, ordered by probability, which will return
    the log-probability of a sample and the sample itself, a set of indices with an active pitch.
    """
    # set up p and not_p probabilities
    p = np.squeeze(weight[0] * acoustic + weight[1] * language)
    not_p = np.squeeze(weight[0] * (1 - acoustic) + weight[1] * (1 - language))
    
    # Base case: most likely chosen greedily
    v_0 = np.where(p > not_p)[0]
    l_0 = np.sum(np.log(np.maximum(p, not_p)))
    yield l_0, v_0
    
    # Sort likelihoods by likelihood penalty for flipping
    L = np.abs(np.log(p / not_p))
    R = L.argsort()
    L_sorted = L[R]
    
    # Num solves crash for duplicate likelihoods
    num = 1
    q = queue.PriorityQueue()
    q.put((L_sorted[0], 0, np.array([0])))
    
    while not q.empty():
        l, _, v = q.get()
        yield l_0 - l, np.setxor1d(v_0, R[v])
        
        i = np.max(v)
        if i + 1 < len(L_sorted):
            q.put((l + L_sorted[i + 1], num, np.append(v, i + 1)))
            q.put((l + L_sorted[i + 1] - L_sorted[i], num+1, np.setxor1d(np.append(v, i + 1), [i])))
            num += 2



            


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("MIDI", help="The MIDI file to load. This should be in the same location as the " +
                        "corresponding acoustic csv, and have the same name (except the extension).")
    
    parser.add_argument("-m", "--model", help="The location of the trained language model.", required=True)
    parser.add_argument("--hidden", help="The number of hidden layers in the language model. Defaults to 256",
                        type=int, default=256)
    
    parser.add_argument("--step", help="Change the step type for frame timing. Either time (default), " +
                        "quant (for 16th notes), or event (for onsets).", default="time")
    
    parser.add_argument("-b", "--beam", help="The beam size. Defaults to 100.", type=int, default=100)
    parser.add_argument("-k", "--branch", help="The branching factor. Defaults to 20.", type=int, default=20)
    
    parser.add_argument("-u", "--union", help="Use the union sampling method.", action="store_true")
    parser.add_argument("-w", "--weight", help="The weight for the acoustic model (between 0 and 1). " +
                        "Defaults to 0.5", type=float, default=0.5)
    
    args = parser.parse_args()
    
    if args.step not in ["time", "quant", "event"]:
        print("Step type must be one of time, quant, or event.", file=sys.stderr)
        sys.exit(1)
        
    if not (0 <= args.weight <= 1):
        print("Weight must be between 0 and 1.", file=sys.stderr)
        sys.exit(2)
    
    # Load data
    data = dataMaps.DataMaps()
    data.make_from_file(args.MIDI, args.step, section=[0, 30])
    
    # Load model
    model_param = make_model_param()
    model_param['n_hidden'] = args.hidden
    model_param['n_steps'] = 1 # To generate 1 step at a time

    # Build model object
    model = Model(model_param)
    sess,_ = model.load(args.model, model_path=args.model)
    
    # Decode
    pr, priors = decode(data.input, model, sess, branch_factor=args.branch, beam_size=args.beam,
                        sampling_method="union" if args.union else "joint", weight=[args.weight, 1 - args.weight])
    
    # Evaluate
    np.save("pr", pr)
    np.save("priors", priors)