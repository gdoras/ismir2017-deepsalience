"""Script to compute training data"""
from __future__ import print_function

import argparse
from joblib import Parallel, delayed
import librosa
import medleydb as mdb
from medleydb import mix
import numpy as np
import os
from scipy.signal import upfirdn
from scipy.ndimage import filters
import sox


def get_hcqt_params():
    """Hack to always use the same parameters :)
    """
    bins_per_octave = 120
    n_octaves = 5
    harmonics = [0.5, 1, 2, 3, 4, 5]
    sr = 22050
    fmin = 32.7
    hop_length = 128
    return bins_per_octave, n_octaves, harmonics, sr, fmin, hop_length


def compute_hcqt(audio_fpath):
    """Compute the harmonic CQT from a given audio file
    """
    (bins_per_octave, n_octaves, harmonics,
     sr, f_min, hop_length) = get_hcqt_params()
    y, fs = librosa.load(audio_fpath, sr=sr)

    cqt_list = []
    shapes = []
    for h in harmonics:
        cqt = librosa.cqt(
            y, sr=fs, hop_length=hop_length, fmin=f_min*float(h),
            n_bins=bins_per_octave*n_octaves,
            bins_per_octave=bins_per_octave
        )
        cqt_list.append(cqt)
        shapes.append(cqt.shape)
    
    shapes_equal = [s == shapes[0] for s in shapes]
    if not all(shapes_equal):
        min_time = np.min([s[1] for s in shapes])
        new_cqt_list = []
        for i, cqt in enumerate(cqt_list):
            new_cqt_list.append(cqt[:, :min_time])
            cqt_list.pop(i)
        cqt_list = new_cqt_list

    log_hcqt = 20.0*np.log10(np.abs(np.array(cqt_list)) + 1.0)
    log_hcqt = log_hcqt - np.min(log_hcqt)
    log_hcqt = log_hcqt / np.max(log_hcqt)
    return log_hcqt


def get_freq_grid():
    """Get the hcqt frequency grid
    """
    (bins_per_octave, n_octaves, _, _, f_min, _) = get_hcqt_params()
    freq_grid = librosa.cqt_frequencies(
        bins_per_octave*n_octaves, f_min, bins_per_octave=bins_per_octave
    )
    return freq_grid


def get_time_grid(n_time_frames):
    """Get the hcqt time grid
    """
    (_, _, _, sr, _, hop_length) = get_hcqt_params()
    time_grid = librosa.core.frames_to_time(
        range(n_time_frames), sr=sr, hop_length=hop_length
    )
    return time_grid


def grid_to_bins(grid, start_bin_val, end_bin_val):
    """Compute the bin numbers from a given grid
    """
    bin_centers = (grid[1:] + grid[:-1])/2.0
    bins = np.concatenate([[start_bin_val], bin_centers, [end_bin_val]])
    return bins


def create_annotation_target(freq_grid, time_grid, annotation_times,
                             annotation_freqs, gaussian_blur):
    """Create the binary annotation target labels
    """
    time_bins = grid_to_bins(time_grid, 0.0, time_grid[-1])
    freq_bins = grid_to_bins(freq_grid, 0.0, freq_grid[-1])

    annot_time_idx = np.digitize(annotation_times, time_bins) - 1
    annot_freq_idx = np.digitize(annotation_freqs, freq_bins) - 1

    n_freqs = len(freq_grid)
    n_times = len(time_grid)

    idx = annot_time_idx < n_times
    annot_time_idx = annot_time_idx[idx]
    annot_freq_idx = annot_freq_idx[idx]

    idx2 = annot_freq_idx < n_freqs
    annot_time_idx = annot_time_idx[idx2]
    annot_freq_idx = annot_freq_idx[idx2]

    annotation_target = np.zeros((n_freqs, n_times))
    annotation_target[annot_freq_idx, annot_time_idx] = 1

    if gaussian_blur:
        annotation_target_blur = filters.gaussian_filter1d(
            annotation_target, 2, axis=0, mode='constant'
        )
        min_target = np.min(
            annotation_target_blur[annot_freq_idx, annot_time_idx]
        )

        annotation_target_blur = annotation_target_blur / min_target
        annotation_target_blur[annotation_target_blur > 1.0] = 1.0

    return annotation_target_blur


def get_annot_activation(annot_data, mtrack_duration):
    annot = np.array(annot_data).T
    times = annot[0]
    freqs = annot[1]

    n_time_frames = int(np.ceil(mtrack_duration * float(44100)))
    time_grid = librosa.core.frames_to_time(
        range(n_time_frames), sr=44100, hop_length=1
    )
    time_bins = grid_to_bins(time_grid, 0.0, time_grid[-1])

    annot_time_idx = np.digitize(times, time_bins) - 1
    annot_time_idx = annot_time_idx[annot_time_idx < len(time_grid)]

    freq_complete = np.zeros(time_grid.shape)
    freq_complete[annot_time_idx] = freqs

    annot_activation = np.array(freq_complete > 0, dtype=float)
    annot_activation = upfirdn(np.ones((256, )), annot_activation)

    # blur the edges
    temp = np.zeros(annot_activation.shape)
    temp += annot_activation
    temp[11025:] += annot_activation[:-11025]
    temp[:-11025] += annot_activation[11025:]

    annot_activation = np.array(temp > 0, dtype=float)

    return annot_activation


def create_filtered_stem(original_audio, output_path, annot_activation):
    sr = 44100
    y, _ = librosa.load(original_audio, sr=sr)

    n_annot = len(annot_activation)
    n_y = len(y)

    if n_annot > n_y:
        annot_activation = annot_activation[:n_y]
    elif n_annot < n_y:
        np.append(annot_activation, np.zeros((n_y - n_annot,)))

    y_out = y * annot_activation
    librosa.output.write_wav(output_path, y_out, sr, norm=False)
    return output_path


def get_all_pitch_annotations(mtrack):
    annot_times = []
    annot_freqs = []
    stems_used = []
    stem_annot_activity = {}
    for stem in mtrack.stems.values():
        data = stem.pitch_annotation
        data2 = stem.pitch_estimate_pyin
        if data is not None:
            annot = data
            stems_used.append(stem.stem_idx)
            stem_annot_activity[stem.stem_idx] = get_annot_activation(
                data, mtrack.duration
            )

        elif data2 is not None:
            annot = data2
            stems_used.append(stem.stem_idx)
        else:
            continue

        annot = np.array(annot).T
        annot_times.append(annot[0])
        annot_freqs.append(annot[1])

    if len(annot_times) > 0:
        annot_times = np.concatenate(annot_times)
        annot_freqs = np.concatenate(annot_freqs)

        return annot_times, annot_freqs, stems_used, stem_annot_activity
    else:
        return None, None, None, stem_annot_activity


def get_input_output_pairs(audio_fpath, annot_times, annot_freqs,
                           gaussian_blur):
    hcqt = compute_hcqt(audio_fpath)

    freq_grid = get_freq_grid()
    time_grid = get_time_grid(len(hcqt[0][0]))

    annot_target = create_annotation_target(
        freq_grid, time_grid, annot_times, annot_freqs, gaussian_blur
    )

    return hcqt, annot_target, freq_grid, time_grid


def save_data(save_path, X, Y, f, t):
    np.savez(save_path, data_in=X, data_out=Y, freq=f, time=t)
    print("    Saved data to {}".format(save_path))


def compute_solo_pitch(mtrack, save_dir, gaussian_blur):
    for stem in mtrack.stems.values():
        data = stem.pitch_annotation
        save_path = os.path.join(
            save_dir, "{}_STEM_{}.npz".format(mtrack.track_id, stem.stem_id)
        )

        if data is None:
            continue
        elif not os.path.exists(stem.audio_path):
            print("        > didn't find audio for this stem")
        else:
            annot = np.array(data).T
            X, Y, f, t = get_input_output_pairs(
                stem.audio_path, annot[0], annot[1], gaussian_blur
            )
            save_data(save_path, X, Y, f, t)


def compute_melody1(mtrack, save_dir, gaussian_blur):
    data = mtrack.melody1_annotation
    if data is None:
        print("    {} No melody 1 data".format(mtrack.track_id))
    else:
        save_path = os.path.join(
            save_dir, "{}_mel1.npz".format(mtrack.track_id)
        )
        if not os.path.exists(save_path):
            annot = np.array(data).T
            times = annot[0]
            freqs = annot[1]

            idx = np.where(freqs != 0.0)[0]

            times = times[idx]
            freqs = freqs[idx]

            X, Y, f, t = get_input_output_pairs(
                mtrack.mix_path, times, freqs, gaussian_blur
            )
            save_data(save_path, X, Y, f, t)
        else:
            print("   {} already computed!".format(mtrack.track_id))


def compute_melody2(mtrack, save_dir, gaussian_blur):
    data = mtrack.melody2_annotation
    if data is None:
        print("    {} No melody 2 data".format(mtrack.track_id))
    else:
        save_path = os.path.join(
            save_dir, "{}_mel2.npz".format(mtrack.track_id)
        )
        if not os.path.exists(save_path):
            annot = np.array(data).T
            times = annot[0]
            freqs = annot[1]

            idx = np.where(freqs != 0.0)[0]

            times = times[idx]
            freqs = freqs[idx]

            X, Y, f, t = get_input_output_pairs(
                mtrack.mix_path, times, freqs, gaussian_blur
            )
            save_data(save_path, X, Y, f, t)
        else:
            print("   {} already computed!".format(mtrack.track_id))


def compute_melody3(mtrack, save_dir, gaussian_blur):
    data = mtrack.melody3_annotation
    if data is None:
        print("   {} No melody 3 data".format(mtrack.track_id))
    else:
        save_path = os.path.join(
            save_dir, "{}_mel3.npz".format(mtrack.track_id)
        )
        if not os.path.exists(save_path):

            annot = np.array(data).T
            times = annot[0]
            all_freqs = annot[1:]
            time_list = []
            freq_list = []
            for i in range(len(all_freqs)):
                time_list.extend(list(times))
                freq_list.extend(list(all_freqs[i]))

            time_list = np.array(time_list)
            freq_list = np.array(freq_list)
            idx = np.where(freq_list != 0.0)[0]

            time_list = time_list[idx]
            freq_list = freq_list[idx]

            X, Y, f, t = get_input_output_pairs(
                mtrack.mix_path, time_list, freq_list, gaussian_blur
            )
            save_data(save_path, X, Y, f, t)
        else:
            print("   {} already computed!".format(mtrack.track_id))


def compute_multif0_incomplete(mtrack, save_dir, gaussian_blur):
    save_path = os.path.join(
        save_dir, "{}_multif0_incomplete.npz".format(mtrack.track_id)
    )
    if not os.path.exists(save_path):

        times, freqs, _, _ = get_all_pitch_annotations(mtrack)

        if times is not None:

            X, Y, f, t = get_input_output_pairs(
                mtrack.mix_path, times, freqs, gaussian_blur
            )

            save_data(save_path, X, Y, f, t)

        else:
            print("    {} No multif0 data".format(mtrack.track_id))

    else:
        print("   {} already computed!".format(mtrack.track_id))


def compute_multif0_complete(mtrack, save_dir, gaussian_blur):
    save_path = os.path.join(
        save_dir, "{}_multif0_complete.npz".format(mtrack.track_id)
    )

    if not os.path.exists(save_path):

        bad_mtrack = False
        for stem in mtrack.stems.values():
            if stem.pitch_estimate_pyin is not None:
                if 'p' in stem.f0_type:
                    bad_mtrack = True
        if bad_mtrack:
            print("multitrack has stems with polyphonic instruments")
            return None

        (times, freqs, stems_used,
         stem_annot_activity) = get_all_pitch_annotations(mtrack)

        if times is not None:
            for i, stem in mtrack.stems.items():
                unvoiced = all([
                    f0_type == 'u' for f0_type in stem.f0_type
                ])
                if unvoiced:
                    stems_used.append(i)

            multif0_mix_path = os.path.join(
                save_dir, "{}_multif0_MIX.wav".format(mtrack.track_id)
            )

            # stems that were manually annotated may not be fully annotated :(
            # silencing out any part of the stem that does not contain
            # annotations just to be safe
            alternate_files = {}
            for key in stem_annot_activity.keys():
                new_stem_path = os.path.join(
                    save_dir, "{}_STEM_{}_alt.wav".format(mtrack.track_id, key)
                )
                created_path = create_filtered_stem(
                    mtrack.stems[key].audio_path, new_stem_path,
                    stem_annot_activity[key]
                )
                alternate_files[key] = created_path

            mix.mix_multitrack(
                mtrack, multif0_mix_path, alternate_files=alternate_files,
                stem_indices=stems_used
            )

            X, Y, f, t = get_input_output_pairs(
                multif0_mix_path, times, freqs, gaussian_blur
            )
            save_data(save_path, X, Y, f, t)

        else:
            print("    {} No multif0 data".format(mtrack.track_id))
    else:
        print("   {} already computed!".format(mtrack.track_id))


def compute_features_mtrack(mtrack, save_dir, option, gaussian_blur):
    print(mtrack.track_id)
    if option == 'solo_pitch':
        compute_solo_pitch(mtrack, save_dir, gaussian_blur)
    elif option == 'melody1':
        compute_melody1(mtrack, save_dir, gaussian_blur)
    elif option == 'melody2':
        compute_melody2(mtrack, save_dir, gaussian_blur)
    elif option == 'melody3':
        compute_melody3(mtrack, save_dir, gaussian_blur)
    elif option == 'multif0_incomplete':
        compute_multif0_incomplete(mtrack, save_dir, gaussian_blur)
    elif option == 'multif0_complete':
        compute_multif0_complete(mtrack, save_dir, gaussian_blur)
    else:
        raise ValueError("Invalid value for `option`.")


def main(args):

    # mtracks = mdb.load_all_multitracks(
    #     dataset_version=['V1', 'V2', 'EXTRA']
    # )
    mtracks = mdb.load_all_multitracks(
        dataset_version=['V1']
    )

    Parallel(n_jobs=-1, verbose=5)(
        delayed(compute_features_mtrack)(
            mtrack, args.save_dir, args.option, args.gaussian_blur
        ) for mtrack in mtracks) 

    print("Done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate feature files for multif0 learning.")
    parser.add_argument("save_dir",
                        type=str,
                        help="Path to save npz files.")
    parser.add_argument("option",
                        type=str,
                        help="Type of data to compute. " +
                        "One of 'solo_pitch' 'melody1', 'melody2', 'melody3' " +
                        "'multif0_incomplete', 'multif0_complete'.")
    parser.add_argument('--blur-labels',
                        dest='gaussian_blur',
                        action='store_true')
    parser.set_defaults(gaussian_blur=False)
    main(parser.parse_args())
