"""Spike analysis
-----------------

Functions to preprocess spike activity and compute statistics.

"""
from mesocircuit.parameterization import helpers_analysis as helpana
from mesocircuit.helpers import helpers
from mesocircuit.helpers import parallelism_time as pt
from mesocircuit.helpers.io import load_h5_to_sparse_X, write_dataset_to_h5_X
from mesocircuit.analysis import stats
from mpi4py import MPI
import os
import warnings
import h5py
import numpy as np
import scipy.sparse as sp
import pickle
import json
from hybridLFPy import helperfun


# initialize MPI
COMM = MPI.COMM_WORLD
SIZE = COMM.Get_size()
RANK = COMM.Get_rank()


def preprocess_data(circuit):
    """
    Converts raw node ids to processed ones, merges raw spike and position
    files, prints a minimal sanity check of the data, performs basic
    preprocessing operations.

    The processed node ids start at 0 for each population.

    The pre-simulation is subtracted from all spike times.

    New .dat files for plain spikes and positions are written and the main
    preprocessed data is stored in .h5 files.

    Parameters
    ----------
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """
    if RANK == 0:
        print('Preprocessing data.')

    # load raw nodeids
    nodeids_raw = _load_raw_nodeids(circuit)

    # write position .dat files with preprocessed node ids:
    num_neurons = pt.parallelize_by_array(circuit.ana_dict['X'],
                                          _convert_raw_file_X,
                                          int,
                                          circuit,
                                          nodeids_raw,
                                          'positions')

    # write spike .dat files with preprocessed node ids:
    num_spikes = pt.parallelize_by_array(circuit.ana_dict['X'],
                                         _convert_raw_file_X,
                                         int,
                                         circuit,
                                         nodeids_raw,
                                         'spike_recorder')

    if not (num_neurons == circuit.net_dict['num_neurons']).all():
        raise Exception('Neuron numbers do not match.')

    # population sizes
    if circuit.ana_dict['extract_1mm2']:
        assert circuit.net_dict['extent'] > 1. / np.sqrt(np.pi), (
            'Disc of 1mm2 cannot be extracted because the extent length '
            'is too small.')
        if RANK == 0:
            print(
                'Extracting data within center disc of 1mm2. '
                'Only the data from these neurons will be analyzed. '
                'Neuron numbers self.N_X will be overwritten.')

        N_X_1mm2 = pt.parallelize_by_array(circuit.ana_dict['X'],
                                           _extract_data_for_1mm2,
                                           int,
                                           circuit)

        if RANK == 0:
            print(f'  Total number of neurons before extraction:\n    '
                  f'{num_neurons}\n'
                  f'  Number of extracted neurons for analysis:\n    '
                  f'{N_X_1mm2}')

        circuit.ana_dict['N_X'] = N_X_1mm2.astype(int)

        # overwrite N_X in ana_dict written to file
        fname = os.path.join(circuit.data_dir_circuit,
                             'parameters', 'ana_dict')
        # pickle for machine readability
        with open(fname + '.pkl', 'wb') as f:
            pickle.dump(circuit.ana_dict, f)
        # text for human readability
        with open(fname + '.txt', 'w') as f:
            json_dump = json.dumps(
                circuit.ana_dict, cls=helpers.NumpyEncoder, indent=2, sort_keys=True)
            f.write(json_dump)

    # minimal analysis as sanity check
    _first_glance_at_data(circuit, num_neurons, num_spikes)

    # preprocess data of each population in parallel
    pt.parallelize_by_array(circuit.ana_dict['X'],
                            _preprocess_data_X,
                            None,
                            circuit)
    return


def compute_statistics(circuit):
    """
    Computes statistics in parallel for each population.

    Parameters
    ----------
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """
    if RANK == 0:
        print('Computing statistics.')

    pt.parallelize_by_array(circuit.ana_dict['X'],
                            _compute_statistics_X,
                            None,
                            circuit)
    return


def merge_h5_files_populations(circuit):
    """
    Merges preprocessed data files and computed statistics for all
    populations.

    Parameters
    ----------
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """
    if RANK == 0:
        print('Merging .h5 files for all populations.')

    pt.parallelize_by_array(circuit.ana_dict['datatypes_preprocess'],
                            _merge_h5_files_populations_datatype,
                            None,
                            circuit)

    pt.parallelize_by_array(circuit.ana_dict['datatypes_statistics'],
                            _merge_h5_files_populations_datatype,
                            None,
                            circuit)
    return


def _load_raw_nodeids(circuit):
    """
    Loads raw node ids from file.

    Parameters
    ----------
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.

    Returns
    -------
    nodeids_raw
        Raw node ids: first and last id per population.
    """
    if RANK == 0:
        print('  Loading raw node ids.')

    # raw node ids: tuples of first and last id of each population;
    # only rank 0 reads from file and broadcasts the data
    if RANK == 0:
        fn = os.path.join(circuit.data_dir_circuit, 'raw_data',
                          circuit.sim_dict['fname_nodeids'])
        nodeids_raw = np.loadtxt(fn, dtype=int)
    else:
        nodeids_raw = None
    nodeids_raw = COMM.bcast(nodeids_raw, root=0)

    return nodeids_raw


def _convert_raw_file_X(i, X, circuit, nodeids_raw, datatype):
    """
    Inner function to be used as argument of pt.parallelize_by_array()
    with array=self.X.
    Corresponding outer function: self._preprocess_data()

    Processes raw network output files (positions, spikes) on HDF5 format.

    Parameters
    ----------
    i: int
        Iterator of populations
        (to be set by outer parallel function).
    X: str
        Population names
        (to be set by outer parallel function).
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    nodeids_raw
        Raw node ids: first and last id per population.
    datatype: str
        Options are 'spike_recorder' and 'positions'.

    Returns
    -------
    num_rows
        An array with the number of rows in the final files.
        datatype = 'spike_recorder': number of spikes per population.
        datatype = 'positions': number of neurons per population
    """
    fname = os.path.join(circuit.data_dir_circuit,
                         'raw_data', f'{datatype}.h5')
    with h5py.File(fname, 'r') as f:
        raw_data = f[X][()]
    sortby = circuit.ana_dict['write_ascii'][datatype]['sortby']
    argsort = np.argsort(raw_data[sortby])
    raw_data = raw_data[argsort]
    raw_data['nodeid'] -= nodeids_raw[i][0]

    dtype = circuit.ana_dict['read_nest_ascii_dtypes'][datatype]
    comb_data = np.empty(raw_data.size, dtype=dtype)
    for name in dtype['names']:
        comb_data[name] = raw_data[name]

    # write processed file (ASCII format)
    fn = os.path.join(circuit.data_dir_circuit,
                      'processed_data', f'{datatype}_{X}.dat')
    np.savetxt(fn, comb_data, delimiter='\t',
               header='\t '.join(dtype['names']),
               fmt=circuit.ana_dict['write_ascii'][datatype]['fmt'])

    return comb_data.size


def _extract_data_for_1mm2(i, X, circuit):
    """
    Extracts positions and spike data for center disc of 1mm2.
    Previous files 'positions_{X}.dat' and 'spike_recorder_{X}.dat' are
    deleted.
    Nodeids in the new data are adjusted such that they are again
    contiguously increasing.

    Parameters
    ----------
    i
        Iterator of populations
        (to be set by outer parallel function).
    X
        Population names
        (to be set by outer parallel function).
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.

    Returns
    -------
    num_neurons_1mm
        Number of neurons in population X within 1mm2.
    """
    spikes, positions = _load_plain_spikes_and_positions(
        X, circuit.data_dir_circuit, circuit.ana_dict['read_nest_ascii_dtypes'])

    spikes_1mm2, positions_1mm2 = \
        _extract_center_disc_1mm2(spikes, positions)

    # delete original data
    for datatype in ['positions', 'spike_recorder']:
        os.remove(os.path.join(circuit.data_dir_circuit,
                               'processed_data', f'{datatype}_{X}.dat'))

    # write 1mm2 positions and spike data
    for datatype in ['positions', 'spike_recorder']:
        dtype = circuit.ana_dict['read_nest_ascii_dtypes'][datatype]
        if datatype == 'positions':
            data = positions_1mm2
        elif datatype == 'spike_recorder':
            data = spikes_1mm2
        # same saving as after converting raw files
        fn = os.path.join(circuit.data_dir_circuit,
                          'processed_data', f'{datatype}_{X}.dat')
        np.savetxt(fn, data, delimiter='\t',
                   header='\t '.join(dtype['names']),
                   fmt=circuit.ana_dict['write_ascii'][datatype]['fmt'])

    num_neurons_1mm2 = len(positions_1mm2)
    return num_neurons_1mm2


def _extract_center_disc_1mm2(spikes, positions):
    """
    Extracts nodeids that belong to the neurons inside 1mm2 center disc of
    radius R: pi * R**2 = 1
    Positions (x,y) with x^2 + y^2  <= 1/pi are accepted.
    Final node ids will be contiguously increasing.

    Parameters
    ----------
    spikes
        Spike data of population X.
    positions
        Positions of population X.

    Returns
    -------
    spikes_1mm2
        Extracted spike data of population X.
    positions_1mm2
        Extracted positions of population X.
    """
    # find nodes within disc
    condition = (positions['x-position_mm']**2 +
                 positions['y-position_mm']**2) <= 1. / np.pi
    node_ids = positions['nodeid'][condition]

    # extracted positions
    positions_1mm2 = positions[condition]
    positions_1mm2['nodeid'] = np.arange(len(node_ids), dtype=int)

    # extracted spike data
    # lookup table for node ids:
    # keys: ids from full set of neurons
    # values: continuous ids for extracted data
    nodeid_lookup = {}
    for id_1mm2, id_full in enumerate(node_ids):
        nodeid_lookup[id_full] = id_1mm2

    spikes_1mm2 = np.zeros_like(spikes)
    cnt = 0
    for sp in spikes:
        if sp['nodeid'] in nodeid_lookup:
            spikes_1mm2[cnt] = \
                np.array((nodeid_lookup[sp['nodeid']], sp['time_ms']),
                         dtype=spikes.dtype)
            cnt += 1
    spikes_1mm2 = spikes_1mm2[:cnt]

    return spikes_1mm2, positions_1mm2


def _first_glance_at_data(circuit, num_neurons, num_spikes):
    """
    Prints a table offering a first glance on the data.

    Parameters
    ----------
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    num_neurons
        Population sizes.
    num_spikes
        An array of spike counts per population.
    """
    # compute firing rates in spikes/s
    rates = num_spikes / num_neurons / \
        ((circuit.sim_dict['t_sim'] + circuit.sim_dict['t_presim']) / 1000.)

    matrix = np.zeros((len(circuit.ana_dict['X']) + 1, 3), dtype=object)
    matrix[0, :] = ['population', 'num_neurons', 'rate_s-1']
    matrix[1:, 0] = circuit.ana_dict['X']
    matrix[1:, 1] = num_neurons.astype(str)
    matrix[1:, 2] = [str(np.around(rate, decimals=3)) for rate in rates]

    title = 'First glance at data'
    pt.print_table(matrix, title)
    return


def _preprocess_data_X(i, X, circuit):
    """
    Inner function to be used as argument of pt.parallelize_by_array()
    with array=self.X.
    Corresponding outer function: self.preprocess_data()

    Each function computing a dataset already writes it to .h5 file.

    Parameters
    ----------
    i
        Iterator of populations
        (to be set by outer parallel function).
    X
        Population names
        (to be set by outer parralel function).
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """

    # get arrays for time and space bins
    time_bins_sim = helpana.get_time_bins(
        circuit.sim_dict['t_presim'], circuit.sim_dict['t_sim'],
        circuit.sim_dict['sim_resolution'])
    time_bins_rs = helpana.get_time_bins(
        circuit.sim_dict['t_presim'], circuit.sim_dict['t_sim'],
        circuit.ana_dict['binsize_time'])
    space_bins = helpana.get_space_bins(
        circuit.net_dict['extent'], circuit.ana_dict['binsize_space'])

    spikes, positions = _load_plain_spikes_and_positions(
        X,
        circuit.data_dir_circuit,
        circuit.ana_dict['read_nest_ascii_dtypes'])

    # order is important as some datasets rely on previous ones
    datasets = {}
    for datatype in circuit.ana_dict['datatypes_preprocess']:
        if i == 0:
            print('  Processing: ' + datatype)

        # overwritten for non-sparse datasets
        is_sparse = True
        dataset_dtype = None

        # positions
        if datatype == 'positions':
            datasets[datatype] = _positions_X(positions)
            is_sparse = False

        # spike trains with a temporal binsize corresponding to the
        # simulation resolution
        elif datatype == 'sptrains':
            datasets[datatype] = _time_binned_sptrains_X(
                circuit.ana_dict['N_X'][i], spikes,
                time_bins_sim, dtype=np.uint8)

        # time-binned spike trains
        elif datatype == 'sptrains_bintime':
            datasets[datatype] = _time_binned_sptrains_X(
                circuit.ana_dict['N_X'][i], spikes,
                time_bins_rs, dtype=np.uint8)

        # time-binned and space-binned spike trains
        elif datatype == 'sptrains_bintime_binspace':
            datasets[datatype] = _time_and_space_binned_sptrains_X(
                datasets['positions'], datasets['sptrains_bintime'],
                space_bins,
                dtype=np.uint16)

        # neuron count in each spatial bin
        elif datatype == 'neuron_count_binspace':
            datasets[datatype] = _neuron_count_per_spatial_bin_X(
                datasets['positions'], space_bins)
            is_sparse = False
            dataset_dtype = int

        # time-binned and space-binned instantaneous rates
        elif datatype == 'inst_rates_bintime_binspace':
            datasets[datatype] = \
                _instantaneous_time_and_space_binned_rates_X(
                    datasets['sptrains_bintime_binspace'],
                    circuit.ana_dict['binsize_time'],
                    datasets['neuron_count_binspace'])

        # position sorting arrays
        elif datatype == 'pos_sorting_arrays':
            datasets[datatype] = _pos_sorting_array_X(
                datasets['positions'], circuit.ana_dict['sorting_axis'])
            is_sparse = False
            dataset_dtype = int

        write_dataset_to_h5_X(
            X, circuit.data_dir_circuit, datatype, datasets[datatype],
            is_sparse, dataset_dtype)
    return


def _load_plain_spikes_and_positions(
        X, data_dir_circuit, read_nest_ascii_dtypes):
    """
    Loads plain spike data and positions.

    Parameters
    ----------
    X
        Population name.
    data_dir_circuit
        Data directory of the circuit.
    read_nest_ascii_dtypes
        Dtypes for reading ASCII files from NEST.

    Returns
    -------
    spikes
        Spike data of population X.
    positions
        Positions of population X.
    """
    data_load = []
    for datatype in read_nest_ascii_dtypes.keys():
        fn = os.path.join(
            data_dir_circuit, 'processed_data', f'{datatype}_{X}.dat')
        # ignore all warnings of np.loadtxt(), target in particular
        # 'Empty input file'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            data = np.loadtxt(
                fn, dtype=read_nest_ascii_dtypes[datatype])
        data_load.append(data)
    spikes, positions = data_load
    return spikes, positions


def _positions_X(positions):
    """
    Brings positions in format for writing.

    Parameters
    ----------
    positions
        Positions of population X.

    Returns
    -------
    pos_dic
        Returns a position dictionary.
    """
    pos_dic = {'x-position_mm': positions['x-position_mm'],
               'y-position_mm': positions['y-position_mm']}
    return pos_dic


def _time_binned_sptrains_X(N_X, spikes, time_bins, dtype):
    """
    Computes a spike train as a histogram with ones for each spike at a
    given time binning.

    Sparse matrix with 'data_row_col':
    data: 1 (for a spike)
    row:  nodeid
    col:  time index (binned times in ms can be obtained by multiplying
            with time resolution = time_bins[1] - time_bins[0])

    Parameters
    ----------
    N_X
        Size of population X.
    spikes: ndarray
        Array of node ids and spike times.
    time_bins: ndarray
        Time bins.
    dtype: type or str
        An integer dtype that fits the data.

    Returns
    -------
    sptrains: scipy.sparse.coo.coo_matrix
        Spike trains as sparse matrix in COOrdinate format.

    """
    # if no spikes were recorded, return an empty sparse matrix
    shape = (N_X, time_bins.size)

    if spikes.size == 0:
        sptrains_bintime = sp.coo_matrix(shape, dtype=dtype)
    else:
        # time bins shifted by one bin as needed by np.digitize()
        dt = time_bins[1] - time_bins[0]
        time_bins_digi = np.r_[time_bins[1:], [time_bins[-1] + dt]]
        # indices of time bins to which each spike time belongs
        time_indices = np.digitize(spikes['time_ms'], time_bins_digi)

        # create COO matrix
        data = np.ones(spikes.size, dtype=dtype)
        sptrains_bintime = sp.coo_matrix(
            (data, (spikes['nodeid'], time_indices)),
            shape=shape, dtype=dtype)

    sptrains_bintime = sptrains_bintime.tocsr().tocoo()
    assert np.all(np.diff(sptrains_bintime.row) >= 0), \
        'row indices must be increasing'
    return sptrains_bintime


def _time_and_space_binned_sptrains_X(positions, sptrains_bintime, space_bins, dtype):
    """
    Computes space-binned spike trains from time-binned spike trains.

    Sparse matrix with 'data_row_col':
    data: number of spikes in spatio-temporal bin
    row:  flat position index
    col:  time index (binned times in ms can be obtained by multiplying
            with time resolution = time_bins[1] - time_bins[0] of sptrains)

    Parameters
    ----------
    positions: dict
        Positions of population X.
    sptrains_bintime: scipy.sparse.coo.coo_matrix
        Time-binned spike trains as sparse matrix in COOrdinate format.
    space_bins:
        Spatial bins.
    dtype: type
        An integer dtype that fits the data.

    Returns
    -------
    sptrains_bintime_binspace: scipy.sparse.csr.csr_matrix
        Spike trains as sparse matrix in Compressed Sparse Row format.
    """
    # match position indices with spatial indices
    pos_x = np.digitize(positions['x-position_mm'].astype(float),
                        space_bins[1:], right=False)
    pos_y = np.digitize(positions['y-position_mm'].astype(float),
                        space_bins[1:], right=False)

    # 2D sparse array with spatial bins flattened to 1D
    map_y, map_x = np.mgrid[0:space_bins.size - 1,
                            0:space_bins.size - 1]
    map_y = map_y.ravel()
    map_x = map_x.ravel()

    assert isinstance(sptrains_bintime, sp.coo_matrix), \
        'sptrains_bintime must be of type scipy.sparse.coo_matrix'
    assert np.all(np.diff(sptrains_bintime.row) >= 0), \
        'sptrains_bintime.row must be in increasing order'

    nspikes = np.asarray(
        sptrains_bintime.sum(axis=1)).flatten().astype(int)
    data = sptrains_bintime.data
    col = sptrains_bintime.col
    row = np.zeros(sptrains_bintime.nnz, dtype=int)
    j = 0
    for i, n in enumerate(nspikes):
        if n > 0:
            try:
                [ind] = np.where(
                    (map_x == pos_x[i]) & (map_y == pos_y[i]))[0]
                row[j:j + n] = ind
            except ValueError:
                mssg = 'Neurons must be on spatial analysis grid.'
                raise NotImplementedError(mssg)
        j += n

    sptrains_csr = sp.coo_matrix(
        (data, (row, col)), shape=(map_x.size, sptrains_bintime.shape[1]),
        dtype=dtype).tocsr()

    assert sptrains_bintime.sum() == sptrains_csr.sum(), \
        'sptrains_bintime.sum()={0} != sptrains_csr.sum()={1}'.format(
        sptrains_bintime.sum(), sptrains_csr.sum())

    return sptrains_csr


def _neuron_count_per_spatial_bin_X(positions, space_bins):
    """
    Counts the number of neurons in each spatial bin.

    Parameters
    ----------
    positions
        Positions of population X.
    space_bins
        Spatial bins.

    Returns
    -------
    pos_hist
        2D-histogram with neuron counts in spatial bins.
    """
    pos_hist = np.histogram2d(positions['y-position_mm'],
                              positions['x-position_mm'],
                              bins=[space_bins, space_bins])[0]
    pos_hist = pos_hist.astype(int)
    return pos_hist


def _instantaneous_time_and_space_binned_rates_X(
        sptrains_bintime_binspace, binsize_time, neuron_count_binspace):
    """
    Computes the time- and space-binned rates averaged over neurons in a
    spatial bin.

    Sparse matrix with 'data_row_col':
    data: instantaneous rate in spikes/s
    row:  flat position index
    col:  time index (binned times in ms can be obtained by multiplying
            with time resolution = time_bins[1] - time_bins[0] of sptrains

    Parameters
    ----------
    sptrains_bintime_binspace
        Time-and space-binned spike trains.
    binsize_time
        Temporal bin size.
    neuron_count_binspace
        2D-histogram with neuron counts in spatial bins.

    Returns
    -------
    inst_rates
        Rates as sparse matrix in Compressed Sparse Row format.
    """
    # number of spikes in each spatio-temporal bin per second
    inst_rates = \
        sptrains_bintime_binspace.astype(float) / (binsize_time * 1E-3)

    # rates per neuron
    # flatten histogram and avoid division by zero
    pos_hist = neuron_count_binspace.flatten()
    pos_inds = np.where(pos_hist > 0)

    inst_rates = inst_rates.tolil()
    inst_rates[pos_inds] = (
        inst_rates[pos_inds].toarray().T / pos_hist[pos_inds]).T

    inst_rates = inst_rates.tocsr()
    return inst_rates


def _pos_sorting_array_X(positions, sorting_axis):
    """
    Computes an array with indices for sorting node ids according to the
    given sorting axis.

    Parameters
    ----------
    positions
        Positions of population X.
    sorting_axis
        Sorting axis (x, y, or None).

    Returns
    -------
    pos_sorting_arrays
        Sorting array.
    """
    if sorting_axis == 'x':
        pos_sorting_arrays = np.argsort(positions['x-position_mm'])
    elif sorting_axis == 'y':
        pos_sorting_arrays = np.argsort(positions['y-position_mm'])
    elif sorting_axis is None:
        pos_sorting_arrays = np.arange(positions.size)
    else:
        raise Exception("Sorting axis is not 'x', 'y' or None.")
    return pos_sorting_arrays


def _compute_statistics_X(i, X, circuit):
    """
    Inner function to be used as argument of pt.parallelize_by_array()
    with array=self.X.
    Corresponding outer function: self.compute_statistics()

    Startup transients are removed from temporal data for computing the
    statistics.

    Parameters
    ----------
    i
        Iterator of populations
        (to be set by outer parallel function).
    X
        Population names
        (to be set by outer parralel function).
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """

    # load preprocessed data
    d = {}
    for datatype in circuit.ana_dict['datatypes_preprocess']:
        datatype_X = datatype + '_' + X
        key = datatype + '_X'
        fn = os.path.join(circuit.data_dir_circuit,
                          'processed_data', f'{datatype_X}.h5')
        data = h5py.File(fn, 'r')
        # load .h5 files with sparse data to csr format
        if isinstance(
                data[X],
                h5py._hl.group.Group) and 'data_row_col' in data[X]:
            data = load_h5_to_sparse_X(X, data)
        else:
            data = data[X]

        # non-temporal data
        if datatype in \
            ['positions',
                'neuron_count_binspace',
                'pos_sorting_arrays']:
            d.update({key: data})

        # remove startup transient from data with simulation resolution
        elif datatype in \
                ['sptrains']:
            d.update({key: data[:, circuit.ana_dict['min_time_index_sim']:]})

        # remove startup transient from temporally binned data
        elif datatype in \
            ['sptrains_bintime',
                'sptrains_bintime_binspace',
                'inst_rates_bintime_binspace']:
            d.update({key: data[:, circuit.ana_dict['min_time_index_rs']:]})

        else:
            raise Exception(
                'Handling undefined for datatype: ', datatype)

    # compute statistics
    # order is important!
    for datatype in circuit.ana_dict['datatypes_statistics']:
        if i == 0:
            print('  Computing: ' + datatype)

        if d['sptrains_X'].size == 0:
            dataset = np.array([])
        else:
            # per-neuron firing rates
            if datatype == 'FRs':
                dataset = _compute_rates(
                    d['sptrains_X'], circuit.ana_dict['time_statistics'])

            # local coefficients of variation
            elif datatype == 'LVs':
                dataset = _compute_lvs(d['sptrains_X'])

            # correlation coefficients with distances
            elif datatype == 'CCs_distances':
                dataset = _compute_ccs_distances(
                    X, circuit,
                    d['sptrains_X'], circuit.sim_dict['sim_resolution'],
                    d['positions_X'])

            # power spectral densities
            elif datatype == 'PSDs':
                dataset = _compute_psds(
                    d['sptrains_bintime_X'], circuit.ana_dict['binsize_time'],
                    circuit.ana_dict['psd_NFFT'])

            # distance-dependent cross-correlation functions with the
            # thalamic population TC only for pulses
            elif datatype == 'CCfuncs_thalamic_pulses':
                if (circuit.net_dict['thalamic_input'] == True and
                    circuit.net_dict['thalamic_input_type'] == 'pulses' and
                        X != 'TC'):
                    # load data from TC
                    fn_TC = os.path.join(circuit.data_dir_circuit, 'processed_data',
                                         'sptrains_bintime_binspace_TC.h5')
                    data_TC = h5py.File(fn_TC, 'r')
                    data_TC = load_h5_to_sparse_X('TC', data_TC)
                    sptrains_bintime_binspace_TC = \
                        data_TC[:, circuit.ana_dict['min_time_index_rs']:]
                    dataset = _compute_cc_funcs_thalamic_pulses(
                        X, circuit, d['sptrains_bintime_binspace_X'],
                        sptrains_bintime_binspace_TC)
                else:
                    dataset = np.array([])

        write_dataset_to_h5_X(
            X, circuit.data_dir_circuit, datatype, dataset, is_sparse=False)
    return


def _compute_rates(sptrains_X, duration):
    """
    Computes the firing rate of each neuron by dividing the spike count by
    the analyzed simulation time.

    Parameters
    ----------
    sptrains_X
        Sptrains of population X in sparse csr format.
    duration
        Length of the time interval of sptrains_X (in ms).

    """
    count = np.array(sptrains_X.sum(axis=1)).flatten()
    rates = count * 1.E3 / duration  # in 1/s
    return rates


def _compute_lvs(sptrains_X):
    """
    Computes local coefficients of variation from inter-spike intervals.

    This function was modified from https://github.com/NeuralEnsemble/elephant

    Calculate the measure of local variation LV for
    a sequence of time intervals between events.
    Given a vector v containing a sequence of intervals, the LV is
    defined as:
    .math $$ LV := \\frac{1}{N}\\sum_{i=1}^{N-1}
                \\frac{3(isi_i-isi_{i+1})^2}
                        {(isi_i+isi_{i+1})^2} $$
    The LV is typically computed as a substitute for the classical
    coefficient of variation for sequences of events which include
    some (relatively slow) rate fluctuation.  As with the CV, LV=1 for
    a sequence of intervals generated by a Poisson process.

    Parameters
    ----------
    sptrains_X
        Sptrains of population X in sparse csr format.

    References
    ----------
    ..[1] Shinomoto, S., Shima, K., & Tanji, J. (2003). Differences in spiking
    patterns among cortical neurons. Neural Computation, 15, 2823-2842

    """
    lvs = np.zeros(sptrains_X.shape[0])
    for i, sptrain in enumerate(sptrains_X):
        # inter-spike intervals of spike trains of individual neurons in
        # units of time steps of sptrains_X
        # (for isis in units of ms or s, multiply with the time step)
        isi = np.diff(np.where(sptrain.toarray())[1])

        if isi.size < 2:
            lvs[i] = np.nan
        else:
            lvs[i] = 3. * (
                np.power(np.diff(isi) / (isi[:-1] + isi[1:]), 2)).mean()
    return lvs


def _compute_ccs_distances(X, circuit, sptrains_X, binsize_time, positions_X):
    """
    Computes Pearson correlation coefficients (excluding auto-correlations)
    and distances between correlated neurons.

    Parameters
    ----------
    X
        Population name.
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    sptrains_X
        Sptrains of population X in sparse csr format.
    binsize_time
        Temporal resolution of sptrains_X (in ms).
    positions_X
        Positions of population X.
    """
    min_num_neurons = np.min(circuit.net_dict['num_neurons'])
    if circuit.ana_dict['ccs_num_neurons'] == 'auto' or \
            circuit.ana_dict['ccs_num_neurons'] > min_num_neurons:
        num_neurons = min_num_neurons
    else:
        num_neurons = circuit.ana_dict['ccs_num_neurons']

    # number of neurons in data
    num_neurons_data = sptrains_X.shape[0]
    # number of time steps in data
    num_timesteps = sptrains_X.shape[1] - 1  # last time bin discarded
    # number of time bins to be combined
    ntbin = int(circuit.ana_dict['ccs_time_interval'] / binsize_time)
    # container for binned spike trains of the selected number
    spt = np.empty((num_neurons, int(num_timesteps/ntbin)), dtype=int)
    mask_nrns = np.zeros(num_neurons_data, dtype=bool)

    # iterate over all neurons in this population.
    # this avoids loading all spike trains into one big dense matrix that
    # might exceed the available memory for long simulations
    i_spt = 0
    for nid in np.arange(num_neurons_data):
        # extract spike train
        spt_nid = sptrains_X.getrow(nid).toarray()[0]
        # discard very last time bin
        spt_nid = spt_nid[:-1]
        # if the spike train is not empty, bin it and add it to selected
        # spike trains
        if np.sum(spt_nid) > 0:
            # bin data
            spt[i_spt] = spt_nid.reshape(1, -1, ntbin).sum(axis=-1)
            i_spt += 1
            # mark that this neuron has spiked
            mask_nrns[nid] = True

        if i_spt == num_neurons:
            break

    # positions of selected selected neurons
    x_pos = positions_X['x-position_mm'][mask_nrns]
    y_pos = positions_X['y-position_mm'][mask_nrns]

    if X == 'L23E':
        print('    Using ' + str(num_neurons) + ' neurons in each ' +
              'population for computing CCs (if no exception given).')
    if num_neurons != i_spt:
        print('    Exception: Computing CCs of ' + X + ' from ' +
              str(i_spt) + ' neurons because not all selected ' +
              str(num_neurons) + ' neurons spiked.')

    ccs = np.corrcoef(spt)

    # mask lower triangle: elements below the k-th diagonal are zeroed
    # (k=1 excludes auto-correlations, k=0 would include them)
    mask = np.triu(np.ones(ccs.shape), k=1).astype(bool)
    ccs = ccs[mask]

    # pairwise-distances between correlated neurons
    xy_pos = np.vstack((x_pos, y_pos)).T
    distances = _pdist_pbc(xy_pos,
                           extent=[circuit.net_dict['extent']] * 2,
                           edge_wrap=True)

    ccs_dic = {'ccs': ccs,
               'distances_mm': distances}
    return ccs_dic


def _pdist_pbc(xy_pos, extent=(1, 1), edge_wrap=False):
    '''Sort of clone of `scipy.spatial.distance.pdist(xy, metric='euclidean')`
    that supports periodic boundary conditions

    Parameters
    ----------
    xy_pos: ndarray
        shape (n, 2) array with x- and y-positions
    extent: len 2 tuple of floats
        (x, y)-extent of boundary
    edge_wrap: bool
        if True, assume periodic boundary conditions. 
        If False [default], produce same output as
        `scipy.spatial.distance.pdist(xy, metric='euclidean')` 

    Returns
    -------
    Y: ndarray
        Returns a condensed distance matrix Y. For each :math:`i` and :math:`j`
        (where :math:`i<j<m`),where m is the number of original observations.
        The metric ``dist(u=X[i], v=X[j])`` is computed and stored in entry ``m
        * i + j - ((i + 2) * (i + 1)) // 2``.
    '''
    d_h = np.array([])
    for i in range(xy_pos.shape[0]):
        d_ = helperfun._calc_radial_dist_to_cell(
            x=xy_pos[i, 0],
            y=xy_pos[i, 1],
            Xpos=xy_pos[i+1:],
            xextent=extent[0],
            yextent=extent[1],
            edge_wrap=edge_wrap
        )
        d_h = np.r_[d_h, d_]
    return d_h


def _compute_psds(sptrains_X, binsize_time, NFFT):
    """
    Computes population-rate power spectral densities.

    Parameters
    ----------
    sptrains_X
        Sptrains of population X in sparse csr format.
    binsize_time
        Temporal resolution of sptrains_X (in ms).
    NFFT
        Number of data points used in each block for the FFT.
    """
    # sampling frequency
    Fs = 1000. / binsize_time

    # compute rate
    x = np.array(sptrains_X.sum(axis=0), dtype=float).flatten()

    Pxx, freq = stats.compute_psd(x, Fs, NFFT)

    # frequencies (in 1/s), PSDs (in s^{-2} / Hz)
    psds = {'frequencies_s-1': freq,
            'psds_s^-2_Hz-1': Pxx}
    return psds


def _compute_cc_funcs_thalamic_pulses(
        X,
        circuit,
        sptrains_bintime_binspace_X,
        sptrains_bintime_binspace_TC):
    """
    Compute distance-dependent cross-correlation functions for thalamic
    pulses.

    Each spatio-temporally binned spike train is correlated with the
    thalamic pulse activity.
    Distances are computed as a function of distance to the center of the
    network.

    Parameters
    ----------
    X
        Population name.
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    sptrains_bintime_binspace_X
        Time- and space-binned spike trains of population X.
    sptrains_bintime_binspace_TC
        Time- and space-binned spike trains of the thalamic population TC.

    Returns
    -------
    cc_func_dic
        Dictionary with cross-correlation functions, distances and time lags.
    """
    # unique spike times of TC neurons, 1s at spike times (else 0s)
    data0 = sptrains_bintime_binspace_TC.toarray()
    data0_prune = np.sum(data0, axis=0)
    data0_prune[np.nonzero(data0_prune)] = 1
    data0_prune = data0_prune.astype(float)  # were integers up to here

    # determine number of bins along the diagonals to be evaluated
    dim = int(np.sqrt(data0.shape[0]))  # number of spatial bin in data
    nbins_diag = circuit.ana_dict['cc_funcs_nbins_diag']
    if nbins_diag > int(dim / 2):
        nbins_diag = int(dim / 2)
    if dim % 2 != 0:
        raise Exception

    if X == 'L23E' and nbins_diag != circuit.ana_dict['cc_funcs_nbins_diag']:
        print('    Using ' + str(nbins_diag) + ' spatial bins for ' +
              'computing CCfuncs_thalmus_center.')

    # time lag indices and lag values
    lag_binsize = circuit.ana_dict['cc_funcs_tau'] / \
        circuit.ana_dict['binsize_time']
    lag_range = np.arange(-lag_binsize, lag_binsize + 1)

    lag_inds = lag_range.astype(int) + int(data0.shape[-1] / 2.)
    lags = lag_range * circuit.ana_dict['binsize_time']

    # distances in mm corresponding to nbins_diag
    distances = np.arange(
        circuit.ana_dict['binsize_space'] / np.sqrt(2.),
        nbins_diag * np.sqrt(2.) * circuit.ana_dict['binsize_space'],
        np.sqrt(2.) * circuit.ana_dict['binsize_space'])

    cc_func = np.zeros((nbins_diag, lags.size))

    data1 = sptrains_bintime_binspace_X.toarray().astype(float)
    dim1 = int(np.sqrt(data1.shape[0]))  # number of spatial bins
    if dim1 != dim:
        raise Exception

    # indeces of diagonal elements, both diagonals
    idx_diag_1 = np.reshape([i * dim1 + i for i in range(dim1)], (2, -1))
    idx_diag_2 = np.reshape([(i + 1) * dim1 - (i + 1)
                             for i in range(dim1)], (2, -1))

    # 4 elements at the same distance from the center
    idx_diag_same_dist = np.array([idx_diag_1[0][::-1],
                                   idx_diag_1[1],
                                   idx_diag_2[0][::-1],
                                   idx_diag_2[1]])

    # go through nbins_diag distances
    for k in np.arange(0, nbins_diag, 1):
        # container
        # 4 diagonal elements at equal distance
        cc = np.zeros((4, lags.size))

        # indices at k diagonal bins distance
        idx_k = idx_diag_same_dist[:, k]

        data1_prune = data1[idx_k, :]

        for j in range(len(idx_k)):
            # correlated entries
            x0 = stats.ztransform(data0_prune)
            x0 /= x0.size

            x1 = stats.ztransform(data1_prune[j, :])

            cc[j, ] = np.correlate(x0, x1, 'same')[lag_inds][::-1]

        cc_func_mean_k = cc.mean(axis=0)

        # for every distance, subtract baseline (mean value before time lag
        # 0)
        idx_bl = np.arange(0, int(len(lags) / 2.))
        baseline_k = np.mean(cc_func_mean_k[idx_bl])
        cc_func[k, :] = cc_func_mean_k - baseline_k

    cc_func_dic = {'cc_funcs': cc_func,
                   'distances_mm': distances,
                   'lags_ms': lags}
    return cc_func_dic


def _merge_h5_files_populations_datatype(i, datatype, circuit):
    """
    Inner function to be used as argument of pt.parallelize_by_array()
    with array=datatypes.
    Corresponding outer function: self._preprocess_data()

    Parameters
    ----------
    i
        Iterator of datatypes
        (to be set by outer parallel function).
    datatype
        Datatype to merge file across populations
        (to be set by outer parralel function).
    circuit
        A mesocircuit.Mesocircuit object with loaded parameters.
    """
    print('  Merging .h5 files: ' + datatype)

    fn = os.path.join(circuit.data_dir_circuit,
                      'processed_data', f'all_{datatype}.h5')

    f = h5py.File(fn, 'w')
    for X in circuit.ana_dict['X']:
        fn_X = os.path.join(circuit.data_dir_circuit,
                            'processed_data', f'{datatype}_{X}.h5')
        f_X = h5py.File(fn_X, 'r')
        f.copy(f_X[X], X)
        f_X.close()
        os.system('rm ' + fn_X)
    f.close()
    return
