"""PyNEST Mesocircuit: SpikeAnalysis Class
------------------------------------------

The SpikeAnalysis Class defines functions to preprocess spike activity and
compute statistics.

"""

from ..helpers import base_class
from ..helpers import parallelism_time as pt
from ..helpers.io import load_h5_to_sparse_X
from . import stats
from mpi4py import MPI
import matplotlib.pyplot as plt
import os
import warnings
import h5py
import numpy as np
import scipy.sparse as sp
import scipy.spatial as spatial

# initialize MPI
COMM = MPI.COMM_WORLD
SIZE = COMM.Get_size()
RANK = COMM.Get_rank()


class SpikeAnalysis(base_class.BaseAnalysisPlotting):
    """
    Provides functions to analyze the spiking data written out by NEST.

    Instantiating a SpikeAnalysis object sets class attributes,
    merges spike and position files, changes node ids, and rescales spike times.
    The processed node ids start at 0 for each population.
    The pre-simulation is subtracted from all spike times.

    Parameters
    ---------
    sim_dict
        Dictionary containing all parameters specific to the simulation
        (derived from: ``base_sim_params.py``).
    net_dict
         Dictionary containing all parameters specific to the neuron and
         network models (derived from: ``base_network_params.py``).
    ana_dict
        Dictionary containing all parameters specific to the network analysis
        (derived from: ``base_analysis_params.py``

    """

    def __init__(self, sim_dict, net_dict, ana_dict):
        """
        Initializes some class attributes.
        """
        if RANK == 0:
            print('Instantiating a SpikeAnalysis object.')

        # inherit from parent class
        super().__init__(sim_dict, net_dict, ana_dict)

        # population sizes
        self.N_X = net_dict['num_neurons']

        return

    def preprocess_data(self):
        """
        Converts raw node ids to processed ones, merges raw spike and position
        files, prints a minimal sanity check of the data, performs basic
        preprocessing operations.

        New .dat files for plain spikes and positions are written and the main
        preprocessed data is stored in .h5 files.
        """
        if RANK == 0:
            print('Preprocessing data.')

        # load raw nodeids
        self.nodeids_raw = self.__load_raw_nodeids()

        # write position .dat files with preprocessed node ids:
        num_neurons = pt.parallelize_by_array(self.X,
                                              self.__convert_raw_file_X,
                                              int,
                                              'positions')

        # write spike .dat files with preprocessed node ids:
        num_spikes = pt.parallelize_by_array(self.X,
                                             self.__convert_raw_file_X,
                                             int,
                                             'spike_recorder')

        if not (num_neurons == self.net_dict['num_neurons']).all():
            raise Exception('Neuron numbers do not match.')

        # population sizes
        if self.ana_dict['extract_1mm2']:
            assert self.net_dict['extent'] > 1. / np.sqrt(np.pi), (
                'Disc of 1mm2 cannot be extracted because the extent length '
                'is too small.')
            if RANK == 0:
                print(
                    'Extracting data within center disc of 1mm2. '
                    'Only the data from these neurons will be analyzed. '
                    'Neuron numbers self.N_X will be overwritten.')

            N_X_1mm2 = pt.parallelize_by_array(self.X,
                                               self.__extract_data_for_1mm2,
                                               int)

            if RANK == 0:
                print(f'  Total number of neurons before extraction:\n    '
                      f'{num_neurons}\n'
                      f'  Number of extracted neurons for analysis:\n    '
                      f'{N_X_1mm2}')

            self.N_X = N_X_1mm2.astype(int)

        # minimal analysis as sanity check
        self.__first_glance_at_data(self.N_X, num_spikes)

        # preprocess data of each population in parallel
        pt.parallelize_by_array(self.X,
                                self.__preprocess_data_X)
        return

    def compute_statistics(self):
        """
        Computes statistics in parallel for each population.
        """
        if RANK == 0:
            print('Computing statistics.')

        pt.parallelize_by_array(self.X,
                                self.__compute_statistics_X)
        return

    def merge_h5_files_populations(self):
        """
        Merges preprocessed data files and computed statistics for all
        populations.
        """
        if RANK == 0:
            print('Merging .h5 files for all populations.')

        pt.parallelize_by_array(self.ana_dict['datatypes_preprocess'],
                                self.__merge_h5_files_populations_datatype)

        pt.parallelize_by_array(self.ana_dict['datatypes_statistics'],
                                self.__merge_h5_files_populations_datatype)
        return

    def compute_psd(self, x, Fs, detrend='mean', overlap=3 / 4):
        """
        Compute power sprectrum `Pxx` of signal `x` using
        matplotlib.mlab.psd function

        Parameters
        ----------
        x: ndarray
            1-D array or sequence
        Fs: float
            sampling frequency
        detrend: {'none', 'mean', 'linear'} or callable, default 'mean'
            detrend data before fft-ing.
        overlap: float
            fraction of NFFT points of overlap between segments
        """
        NFFT = self.ana_dict['psd_NFFT']
        noverlap = int(overlap * NFFT)
        return plt.mlab.psd(x, NFFT=NFFT, Fs=Fs,
                            detrend=detrend, noverlap=noverlap)

    def __load_raw_nodeids(self):
        """
        Loads raw node ids from file.

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
            fn = os.path.join('raw_data', self.sim_dict['fname_nodeids'])
            nodeids_raw = np.loadtxt(fn, dtype=int)
        else:
            nodeids_raw = None
        nodeids_raw = COMM.bcast(nodeids_raw, root=0)

        return nodeids_raw

    def __convert_raw_file_X(self, i, X, datatype):
        """
        Inner function to be used as argument of pt.parallelize_by_array()
        with array=self.X.
        Corresponding outer function: self.__preprocess_data()

        Processes raw network output files (positions, spikes) on HDF5 format.

        Parameters
        ----------
        i: int
            Iterator of populations
            (to be set by outer parallel function).
        X: str
            Population names
            (to be set by outer parallel function).
        datatype: str
            Options are 'spike_recorder' and 'positions'.

        Returns
        -------
        num_rows
            An array with the number of rows in the final files.
            datatype = 'spike_recorder': number of spikes per population.
            datatype = 'positions': number of neurons per population
        """
        fname = os.path.join('raw_data', f'{datatype}.h5')
        with h5py.File(fname, 'r') as f:
            raw_data = f[X][()]
        sortby = self.ana_dict['write_ascii'][datatype]['sortby']
        argsort = np.argsort(raw_data[sortby])
        raw_data = raw_data[argsort]
        raw_data['nodeid'] -= self.nodeids_raw[i][0]

        dtype = self.ana_dict['read_nest_ascii_dtypes'][datatype]
        comb_data = np.empty(raw_data.size, dtype=dtype)
        for name in dtype['names']:
            comb_data[name] = raw_data[name]

        # write processed file (ASCII format)
        fn = os.path.join('processed_data', f'{datatype}_{X}.dat')
        np.savetxt(fn, comb_data, delimiter='\t',
                   header='\t '.join(dtype['names']),
                   fmt=self.ana_dict['write_ascii'][datatype]['fmt'])

        return comb_data.size

    def __extract_data_for_1mm2(self, i, X):
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

        Returns
        -------
        num_neurons_1mm
            Number of neurons in population X within 1mm2.
        """
        spikes, positions = self.__load_plain_spikes_and_positions(X)

        spikes_1mm2, positions_1mm2 = \
            self._extract_center_disc_1mm2(spikes, positions)

        # delete original data
        for datatype in ['positions', 'spike_recorder']:
            os.remove(os.path.join('processed_data', f'{datatype}_{X}.dat'))

        # write 1mm2 positions and spike data
        for datatype in ['positions', 'spike_recorder']:
            dtype = self.ana_dict['read_nest_ascii_dtypes'][datatype]
            if datatype == 'positions':
                data = positions_1mm2
            elif datatype == 'spike_recorder':
                data = spikes_1mm2
            # same saving as after converting raw files
            fn = os.path.join('processed_data', f'{datatype}_{X}.dat')
            np.savetxt(fn, data, delimiter='\t',
                       header='\t '.join(dtype['names']),
                       fmt=self.ana_dict['write_ascii'][datatype]['fmt'])

        num_neurons_1mm2 = len(positions_1mm2)
        return num_neurons_1mm2

    def _extract_center_disc_1mm2(self, spikes, positions):
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

    def __first_glance_at_data(self, N_X, num_spikes):
        """
        Prints a table offering a first glance on the data.

        Parameters
        ----------
        N_X
            Population sizes.
        num_spikes
            An array of spike counts per population.
        """
        # compute firing rates in 1/s
        rates = num_spikes / N_X / \
            ((self.sim_dict['t_sim'] + self.sim_dict['t_presim']) / 1000.)

        matrix = np.zeros((len(self.X) + 1, 3), dtype=object)
        matrix[0, :] = ['population', 'num_neurons', 'rate_s-1']
        matrix[1:, 0] = self.X
        matrix[1:, 1] = N_X.astype(str)
        matrix[1:, 2] = [str(np.around(rate, decimals=3)) for rate in rates]

        title = 'First glance at data'
        pt.print_table(matrix, title)
        return

    def __preprocess_data_X(self, i, X):
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
        """

        spikes, positions = self.__load_plain_spikes_and_positions(X)

        # order is important as some datasets rely on previous ones
        datasets = {}
        for datatype in self.ana_dict['datatypes_preprocess']:
            if i == 0:
                print('  Processing: ' + datatype)

            # overwritten for non-sparse datasets
            is_sparse = True
            dataset_dtype = None

            # positions
            if datatype == 'positions':
                datasets[datatype] = self.__positions_X(
                    X, positions)
                is_sparse = False

            # spike trains with a temporal binsize corresponding to the
            # simulation resolution
            elif datatype == 'sptrains':
                datasets[datatype] = self.__time_binned_sptrains_X(
                    X, spikes, self.time_bins_sim, dtype=np.uint8)

            # time-binned spike trains
            elif datatype == 'sptrains_bintime':
                datasets[datatype] = self.__time_binned_sptrains_X(
                    X, spikes, self.time_bins_rs, dtype=np.uint8)

            # time-binned and space-binned spike trains
            elif datatype == 'sptrains_bintime_binspace':
                datasets[datatype] = self._time_and_space_binned_sptrains_X(
                    X, datasets['positions'], datasets['sptrains_bintime'],
                    dtype=np.uint16)

            # neuron count in each spatial bin
            elif datatype == 'neuron_count_binspace':
                datasets[datatype] = self.__neuron_count_per_spatial_bin_X(
                    X, datasets['positions'])
                is_sparse = False
                dataset_dtype = int

            # time-binned and space-binned instantaneous rates
            elif datatype == 'inst_rates_bintime_binspace':
                datasets[datatype] = \
                    self.__instantaneous_time_and_space_binned_rates_X(
                        X, datasets['sptrains_bintime_binspace'],
                        self.ana_dict['binsize_time'],
                        datasets['neuron_count_binspace'])

            # position sorting arrays
            elif datatype == 'pos_sorting_arrays':
                datasets[datatype] = self.__pos_sorting_array_X(
                    X, datasets['positions'])
                is_sparse = False
                dataset_dtype = int

            self.write_dataset_to_h5_X(
                X, datatype, datasets[datatype], is_sparse, dataset_dtype)
        return

    def __load_plain_spikes_and_positions(self, X):
        """
        Loads plain spike data and positions.

        Parameters
        ----------
        X
            Population name.

        Returns
        -------
        spikes
            Spike data of population X.
        positions
            Positions of population X.
        """
        data_load = []
        for datatype in self.ana_dict['read_nest_ascii_dtypes'].keys():
            fn = os.path.join('processed_data', f'{datatype}_{X}.dat')
            # ignore all warnings of np.loadtxt(), target in particular
            # 'Empty input file'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                data = np.loadtxt(
                    fn, dtype=self.ana_dict['read_nest_ascii_dtypes'][datatype])
            data_load.append(data)
        spikes, positions = data_load
        return spikes, positions

    def __positions_X(self, X, positions):
        """
        Brings positions in format for writing.

        Parameters
        ----------
        X
            Population name.
        positions
            Positions of population X.
        """
        pos_dic = {'x-position_mm': positions['x-position_mm'],
                   'y-position_mm': positions['y-position_mm']}
        return pos_dic

    def __time_binned_sptrains_X(self, X, spikes, time_bins, dtype):
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
        X: str
            Population name.
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
        i = np.where(self.X == X)[0][0]  # TODO
        shape = (self.N_X[i], time_bins.size)

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

    def _time_and_space_binned_sptrains_X(
            self, X, positions, sptrains_bintime, dtype):
        """
        Computes space-binned spike trains from time-binned spike trains.

        Sparse matrix with 'data_row_col':
        data: number of spikes in spatio-temporal bin
        row:  flat position index
        col:  time index (binned times in ms can be obtained by multiplying
              with time resolution = time_bins[1] - time_bins[0] of sptrains)

        Parameters
        ----------
        X: str
            Population name.
        positions: dict
            Positions of population X.
        sptrains_bintime: scipy.sparse.coo.coo_matrix
            Time-binned spike trains as sparse matrix in COOrdinate format
        dtype: type
            An integer dtype that fits the data.

        Returns
        -------
        sptrains_bintime_binspace: scipy.sparse.csr.csr_matrix
            Spike trains as sparse matrix in Compressed Sparse Row format.
        """
        # match position indices with spatial indices
        pos_x = np.digitize(positions['x-position_mm'].astype(float),
                            self.space_bins[1:], right=False)
        pos_y = np.digitize(positions['y-position_mm'].astype(float),
                            self.space_bins[1:], right=False)

        # 2D sparse array with spatial bins flattened to 1D
        map_y, map_x = np.mgrid[0:self.space_bins.size - 1,
                                0:self.space_bins.size - 1]
        map_y = map_y.ravel()
        map_x = map_x.ravel()

        assert isinstance(sptrains_bintime, sp.coo_matrix), \
            'sptrains_bintime must be of type scipy.sparse.coo_matrix'
        assert np.all(np.diff(sptrains_bintime.row) >= 0), \
            'sptrains_bintime.row must be in increasing order'

        nspikes = np.asarray(
            sptrains_bintime.sum(
                axis=1)).flatten().astype(int)
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
                    # TODO: ignore spike events from units outside spatial grid
                    mssg = 'neurons must be on spatial analysis grid'
                    raise NotImplementedError(mssg)
            j += n

        sptrains_csr = sp.coo_matrix(
            (data, (row, col)), shape=(map_x.size, sptrains_bintime.shape[1])
        ).tocsr()

        try:
            assert (sptrains_bintime.sum() == sptrains_csr.sum())
        except AssertionError as ae:
            raise ae(
                'sptrains_bintime.sum()={0} != sptrains_coo.sum()={1}'.format(
                    sptrains_bintime.sum(), sptrains_csr.sum()))

        return sptrains_csr

    def __neuron_count_per_spatial_bin_X(self, X, positions):
        """
        Counts the number of neurons in each spatial bin.

        Parameters
        ----------
        X
            Population name.
        positions
            Positions of population X.

        Returns
        -------
        pos_hist
            2D-histogram with neuron counts in spatial bins.
        """
        pos_hist = np.histogram2d(positions['y-position_mm'],
                                  positions['x-position_mm'],
                                  bins=[self.space_bins, self.space_bins])[0]
        pos_hist = pos_hist.astype(int)
        return pos_hist

    def __instantaneous_time_and_space_binned_rates_X(
            self, X, sptrains_bintime_binspace, binsize_time, neuron_count_binspace):
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
        X
            Population name.
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

    def __pos_sorting_array_X(self, X, positions):
        """
        Computes an array with indices for sorting node ids according to the
        given sorting axis.

        Parameters
        ----------
        X
            Population name.
        positions
            Positions of population X.

        Returns
        -------
        pos_sorting_arrays
            Sorting array.
        """
        if self.ana_dict['sorting_axis'] == 'x':
            pos_sorting_arrays = np.argsort(positions['x-position_mm'])
        elif self.ana_dict['sorting_axis'] == 'y':
            pos_sorting_arrays = np.argsort(positions['y-position_mm'])
        elif self.ana_dict['sorting_axis'] is None:
            pos_sorting_arrays = np.arange(positions.size)
        else:
            raise Exception("Sorting axis is not 'x', 'y' or None.")
        return pos_sorting_arrays

    def __compute_statistics_X(self, i, X):
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
        """

        # load preprocessed data
        d = {}
        for datatype in self.ana_dict['datatypes_preprocess']:
            datatype_X = datatype + '_' + X
            key = datatype + '_X'
            fn = os.path.join('processed_data', f'{datatype_X}.h5')
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
                d.update({key: data[:, self.min_time_index_sim:]})

            # remove startup transient from temporally binned data
            elif datatype in \
                ['sptrains_bintime',
                 'sptrains_bintime_binspace',
                 'inst_rates_bintime_binspace']:
                d.update({key: data[:, self.min_time_index_rs:]})

            else:
                raise Exception(
                    'Handling undefined for datatype: ', datatype)

        # compute statistics
        # order is important!
        for datatype in self.ana_dict['datatypes_statistics']:
            if i == 0:
                print('  Computing: ' + datatype)

            if d['sptrains_X'].size == 0:
                dataset = np.array([])
            else:
                # per-neuron firing rates
                if datatype == 'FRs':
                    dataset = self.__compute_rates(
                        X, d['sptrains_X'], self.time_statistics)

                # local coefficients of variation
                elif datatype == 'LVs':
                    dataset = self.__compute_lvs(X, d['sptrains_X'])

                # correlation coefficients with distances
                elif datatype == 'CCs_distances':
                    dataset = self.__compute_ccs_distances(
                        X, d['sptrains_X'], self.sim_dict['sim_resolution'],
                        d['positions_X'])

                # power spectral densities
                elif datatype == 'PSDs':
                    dataset = self.__compute_psds(
                        X, d['sptrains_bintime_X'], self.ana_dict['binsize_time'])

                # distance-dependent cross-correlation functions with the
                # thalamic population TC only for pulses
                elif datatype == 'CCfuncs_thalamic_pulses':
                    if self.net_dict['thalamic_input'] == 'pulses' and X != 'TC':
                        # load data from TC
                        fn_TC = os.path.join('processed_data',
                                             'sptrains_bintime_binspace_TC.h5')
                        data_TC = h5py.File(fn_TC, 'r')
                        data_TC = load_h5_to_sparse_X('TC', data_TC)
                        sptrains_bintime_binspace_TC = \
                            data_TC[:, self.min_time_index_rs:]
                        dataset = self.__compute_cc_funcs_thalamic_pulses(
                            X, d['sptrains_bintime_binspace_X'],
                            sptrains_bintime_binspace_TC)
                    else:
                        dataset = np.array([])

            self.write_dataset_to_h5_X(X, datatype, dataset, is_sparse=False)
        return

    def __compute_rates(self, X, sptrains_X, duration):
        """
        Computes the firing rate of each neuron by dividing the spike count by
        the analyzed simulation time.

        Parameters
        ----------
        X
            Population name.
        sptrains_X
            Sptrains of population X in sparse csr format.
        duration
            Length of the time interval of sptrains_X (in ms).

        """
        count = np.array(sptrains_X.sum(axis=1)).flatten()
        rates = count * 1.E3 / duration  # in 1/s
        return rates

    def __compute_lvs(self, X, sptrains_X):
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
        X
            Population name.
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

    def __compute_ccs_distances(
            self,
            X,
            sptrains_X,
            binsize_time,
            positions_X):
        """
        Computes Pearson correlation coefficients (excluding auto-correlations)
        and distances between correlated neurons.

        Parameters
        ----------
        X
            Population name.
        sptrains_X
            Sptrains of population X in sparse csr format.
        binsize_time
            Temporal resolution of sptrains_X (in ms).
        positions_X
            Positions of population X.
        """
        min_num_neurons = np.min(self.net_dict['num_neurons'])
        if self.ana_dict['ccs_num_neurons'] == 'auto' or \
                self.ana_dict['ccs_num_neurons'] > min_num_neurons:
            num_neurons = min_num_neurons
        else:
            num_neurons = self.ana_dict['ccs_num_neurons']

        # spike trains and node ids
        spt = sptrains_X.toarray()
        spt = spt[:, :-1]  # discard very last time bin
        num_neurons_data = sptrains_X.shape[0]

        # mask out non-spiking neurons
        mask_nrns = np.ones(num_neurons_data, dtype=bool)
        mask_nrns[np.all(spt == 0, axis=1)] = False

        # extract at most num_neurons neurons from spike trains and from
        # positions
        spt = spt[mask_nrns][:num_neurons]
        x_pos = positions_X['x-position_mm'][mask_nrns][:num_neurons]
        y_pos = positions_X['y-position_mm'][mask_nrns][:num_neurons]

        # number of spiking neurons included
        num_neurons_spk = np.shape(spt)[0]

        if X == 'L23E':
            print('    Using ' + str(num_neurons) + ' neurons in each ' +
                  'population for computing CCs (if no exception given).')
        if num_neurons != num_neurons_spk:
            print('    Exception: Computing CCs of ' + X + ' from ' +
                  str(num_neurons_spk) + ' neurons because not all selected ' +
                  str(num_neurons) + ' neurons spiked.')

        # bin spike data according to given interval
        ntbin = int(self.ana_dict['ccs_time_interval'] / binsize_time)
        spt = spt.reshape(num_neurons_spk, -1, ntbin).sum(axis=-1)

        ccs = np.corrcoef(spt)

        # mask lower triangle: elements below the k-th diagonal are zeroed
        # (k=1 excludes auto-correlations, k=0 would include them)
        mask = np.triu(np.ones(ccs.shape), k=1).astype(bool)
        ccs = ccs[mask]

        # pair-distances between correlated neurons]
        xy_pos = np.vstack((x_pos, y_pos)).T
        distances = spatial.distance.pdist(xy_pos, metric='euclidean')

        ccs_dic = {'ccs': ccs,
                   'distances_mm': distances}
        return ccs_dic

    def __compute_psds(self, X, sptrains_X, binsize_time):
        """
        Computes population-rate power spectral densities.

        Parameters
        ----------
        X
            Population name.
        sptrains_X
            Sptrains of population X in sparse csr format.
        binsize_time
            Temporal resolution of sptrains_X (in ms).
        """
        # sampling frequency
        Fs = 1000. / binsize_time

        # compute rate
        x = np.array(sptrains_X.sum(axis=0), dtype=float).flatten()

        Pxx, freq = self.compute_psd(x, Fs)

        # frequencies (in 1/s), PSDs (in s^{-2} / Hz)
        psds = {'frequencies_s-1': freq,
                'psds_s^-2_Hz-1': Pxx}
        return psds

    def __compute_cc_funcs_thalamic_pulses(
            self,
            X,
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
        nbins_diag = self.ana_dict['cc_funcs_nbins_diag']
        if nbins_diag > int(dim / 2):
            nbins_diag = int(dim / 2)
        if dim % 2 != 0:
            raise Exception

        if X == 'L23E' and nbins_diag != self.ana_dict['cc_funcs_nbins_diag']:
            print('    Using ' + str(nbins_diag) + ' spatial bins for ' +
                  'computing CCfuncs_thalmus_center.')

        # time lag indices and lag values
        lag_binsize = self.ana_dict['cc_funcs_tau'] / \
            self.ana_dict['binsize_time']
        lag_range = np.arange(-lag_binsize, lag_binsize + 1)

        lag_inds = lag_range.astype(int) + int(data0.shape[-1] / 2.)
        lags = lag_range * self.ana_dict['binsize_time']

        # distances in mm corresponding to nbins_diag
        distances = np.arange(
            self.ana_dict['binsize_space'] / np.sqrt(2.),
            nbins_diag * np.sqrt(2.) * self.ana_dict['binsize_space'],
            np.sqrt(2.) * self.ana_dict['binsize_space'])

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

    def __merge_h5_files_populations_datatype(self, i, datatype):
        """
        Inner function to be used as argument of pt.parallelize_by_array()
        with array=datatypes.
        Corresponding outer function: self.__preprocess_data()

        Parameters
        ----------
        i
            Iterator of datatypes
            (to be set by outer parallel function).
        datatype
            Datatype to merge file across populations
            (to be set by outer parralel function).
        """
        print('  Merging .h5 files: ' + datatype)

        fn = os.path.join('processed_data', f'all_{datatype}.h5')

        f = h5py.File(fn, 'w')
        for X in self.X:
            fn_X = os.path.join('processed_data', f'{datatype}_{X}.h5')
            f_X = h5py.File(fn_X, 'r')
            f.copy(f_X[X], X)
            f_X.close()
            os.system('rm ' + fn_X)
        f.close()
        return
