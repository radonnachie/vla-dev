import logging
import socket
import numpy as np
import struct
import time
import datetime
import os
import casperfpga
from . import helpers
from . import __version__
from .error_levels import *
from .blocks import block
from .blocks import fpga
from .blocks import sync
from .blocks import input
from .blocks import noisegen
from .blocks import qsfp
from .blocks import dts
from .blocks import pfb
from .blocks import vacc
from .blocks import eth
from .blocks import eq
from .blocks import eqtvg
from .blocks import chanreorder
from .blocks import packetizer
from .blocks import autocorr

FENG_UDP_SOURCE_PORT = 10000
MAC_BASE = 0x020203030400
IP_BASE = (100 << 24) + (100 << 16) + (101 << 8) + 10
NIFS = 2
FPGA_CLOCK_RATE_HZ = 256000000
FIRMWARE_TYPE_8BIT = 2
FIRMWARE_TYPE_3BIT = 3
DEFAULT_FIRMWARE_TYPE = FIRMWARE_TYPE_8BIT

class CosmicFengine():
    """
    A control class for COSMIC's F-Engine firmware.

    :param host: CasperFpga interface for host. If this is of the form `pcieAB`,
        then it is assumed we are connecting to the FPGA card with pcie enumeration
        `0xAB` via a REST server at the supplied `remote_uri`. If `host` is of the
        form `xdmaA`, then we are connecting to the FPGA card with xdma driver
        ID `xdmaA`. In this case, connection may be direct, or via a REST server.
    :type host: casperfpga.CasperFpga

    :param remote_uri: REST host address, eg. `https://100.100.100.100:5000`
    :type remote_uri: str

    :param fpgfile: .fpg file for firmware to program (or already loaded)
    :type fpgfile: str

    :param pipeline_id: Zero-indexed ID of the pipeline on this host.
    :type pipeline_id: int

    :param fpga_id: Zero-indexed ID of the fpga card. I.e., xdma driver instance number
    :type fpga_id: int

    :param neths: Number of 100GbE interfaces for this pipeline.
    :type neths: int

    :param logger: Logger instance to which log messages should be emitted.
    :type logger: logging.Logger

    """
    def __init__(self, host, fpgfile, pipeline_id=0, neths=1, logger=None, remote_uri=None):
        self.hostname = host #: hostname of the F-Engine's host SNAP2 board
        self.pci_id = host[4:] if host.startswith('pcie') else None
        self.instance_id = int(host[4:]) if host.startswith('xdma') else 0
        self.pipeline_id = pipeline_id
        self.fpgfile = fpgfile
        self.neths = neths
        #: Python Logger instance
        self.logger = logger or helpers.add_default_log_handlers(logging.getLogger(__name__ + ":%s" % (host)))
        #: Underlying CasperFpga control instance
        if remote_uri is None:
            self._cfpga = casperfpga.CasperFpga(
                            host=self.hostname,
                            instance_id=self.instance_id,
                            transport=casperfpga.LocalPcieTransport,
                        )
        else:
            self._cfpga = casperfpga.CasperFpga(
                            host=self.hostname,
                            pci_id=self.pci_id,
                            instance_id=self.instance_id,
                            uri=remote_uri,
                            transport=casperfpga.RemotePcieTransport,
                        )
        
            remotepcie = self._cfpga.transport
            if True: #remotepcie.is_connected(0, 0) and not remotepcie.is_programmed():
                print("Programmed Successfully:", remotepcie.upload_to_ram_and_program(fpgfile))

        try:
            self._cfpga.get_system_information(fpgfile)
        except:
            self.logger.error("Failed to read and decode .fpg header %s" % fpgfile)
        self.blocks = {}
        try:
            self._initialize_blocks()
        except:
            self.logger.exception("Failed to inialize firmware blocks. "
                                  "Maybe the board needs programming.")

    def is_connected(self):
        """
        :return: True if there is a working connection to a SNAP2. False otherwise.
        :rtype: bool
        """
        return self._cfpga.is_connected()

    def _initialize_blocks(self):
        """
        Initialize firmware blocks, populating the ``blocks`` attribute.
        Choose initialization method based on firmware type
        """

        f = fpga.Fpga(self._cfpga, "")
        if f.is_programmed():
            firmware_type = f.get_firmware_type()
            self.logger.info("FPGA is programmed with firmware type %d" % firmware_type)
        else:
            firmware_type = DEFAULT_FIRMWARE_TYPE
            self.logger.info("FPGA is not programmed. Defaulting to firmware type %d" % firmware_type)

        if firmware_type == FIRMWARE_TYPE_8BIT:
            self.logger.info("Initializing control blocks for 8-bit mode")
            self._initialize_blocks_8bit()
        elif firmware_type == FIRMWARE_TYPE_3BIT:
            self.logger.info("Initializing control blocks for 3-bit mode")
            self._initialize_blocks_3bit()
        else:
            self.logger.error("Can't initialize firmware control blocks because "
                    "firmware version %d is not known" % firmware_type)

    def _initialize_blocks_8bit(self):
        """
        Initialize firmware blocks, populating the ``blocks`` attribute.
        """
        NCHANS = 512

        # blocks
        #: Control interface to high-level FPGA functionality
        self.fpga        = fpga.Fpga(self._cfpga, "")

        #: Control interface to timing sync block
        self.sync        = sync.Sync(self._cfpga,
                'pipeline%d_sync' % self.pipeline_id,
                clk_hz = FPGA_CLOCK_RATE_HZ)

        #: QSFP ports
        self.qsfp_a      = qsfp.Qsfp(self._cfpga, 'qsfpa')
        self.qsfp_b      = qsfp.Qsfp(self._cfpga, 'qsfpb')
        self.qsfp_c      = qsfp.Qsfp(self._cfpga, 'qsfpc')
        self.qsfp_d      = qsfp.Qsfp(self._cfpga, 'qsfpd')

        self.dts         = dts.Dts(self._cfpga, 'pipeline%d_dts' % self.pipeline_id)

        self.pfb         = pfb.Pfb(self._cfpga, 'pipeline%d_pfb' % self.pipeline_id)

        #: Control interface for the Autocorrelation block
        self.autocorr = autocorr.AutoCorr(self._cfpga,
                'pipeline%d_autocorr' % self.pipeline_id,
                n_chans=NCHANS, n_signals=4, n_parallel_streams=4,
                n_cores=4, use_mux=False)

        #: Control interface to 100GbE interface blocks
        self.eths = []
        for i in range(self.neths):
            self.eths += [eth.Eth(self._cfpga, 'pipeline%d_eth%d' % (self.pipeline_id, i))]

        #: Control interface to Input multiplexor / statistics
        self.input = input.Input(self._cfpga, 'pipeline%d_input' % self.pipeline_id,
                n_inputs=4, n_bits=8, n_parallel_samples=8)

        #: Control interface to noise generation block
        self.noisegen = noisegen.NoiseGen(self._cfpga, 'pipeline%d_noise' % self.pipeline_id,
                n_noise=2, n_outputs=4, n_parallel_samples=8)

        #: Control interface to Equalization block
        self.eq = eq.Eq(self._cfpga, 'pipeline%d_eq' % self.pipeline_id,
                n_streams=4, n_coeffs=2**7)

        #: Control interface to post-equalization Test Vector Generator block
        self.eqtvg = eqtvg.EqTvg(self._cfpga, 'pipeline%d_post_eq_tvg' % self.pipeline_id,
                n_inputs=4, n_serial_inputs=1, n_chans=NCHANS)

        #: Control interface to channel reorder block
        self.chanreorder = chanreorder.ChanReorder(self._cfpga, 'pipeline%d_reorder' % self.pipeline_id,
                n_times=64, n_ants=4, n_chans=NCHANS, n_parallel_chans=16)

        #: Control interface to Packetizerblock
        # 8 signals = 4 IFs (only half are real)
        self.packetizer = packetizer.Packetizer(self._cfpga,
                'pipeline%d_packetizer' % self.pipeline_id,
                n_chans=512, n_ants=4, sample_rate_mhz=2048,
                sample_width=2, word_width=64, line_rate_gbps=100.,
                n_time_packet=64, granularity=4)

        # The order here can be important, blocks are initialized in the
        # order they appear here

        #: Dictionary of all control blocks in the firmware system.
        self.blocks = {
            'dts'         : self.dts,
            'fpga'        : self.fpga,
            'sync'        : self.sync,
            'noisegen'    : self.noisegen,
            'input'       : self.input,
            'qsfp_a'      : self.qsfp_a,
            'qsfp_b'      : self.qsfp_b,
            'qsfp_c'      : self.qsfp_c,
            'qsfp_d'      : self.qsfp_d,
            'dts'         : self.dts,
            'pfb'         : self.pfb,
            'autocorr'    : self.autocorr,
            'eq'          : self.eq,
            'eqtvg'       : self.eqtvg,
            'chanreorder' : self.chanreorder,
            'eths'        : self.eths,
        }

    def initialize(self, read_only=True):
        """
        Call the ```initialize`` methods of all underlying blocks, then
        optionally issue a software global reset.

        :param read_only: If True, call the underlying initialization methods
            in a read_only manner, and skip software reset.
        :type read_only: bool
        """
        for blockname, b in self.blocks.items():
            if read_only:
                self.logger.info("Initializing block (read only): %s" % blockname)
            else:
                self.logger.info("Initializing block (writable): %s" % blockname)
            if isinstance(b, block.Block):
                b.initialize(read_only=read_only)
            elif isinstance(b, list):
                for bi in b:
                    if isinstance(bi, block.Block):
                        bi.initialize(read_only=read_only)
        if not read_only:
            self.logger.info("Performing software global reset")
            #self.sync.arm_sync()
            #self.sync.sw_sync()

    def get_status_all(self):
        """
        Call the ``get_status`` methods of all blocks in ``self.blocks``.
        If the FPGA is not programmed with F-engine firmware, will only
        return basic FPGA status.

        :return: (status_dict, flags_dict) tuple.
            Each is a dictionary, keyed by the names of the blocks in
            ``self.blocks``. These dictionaries contain, respectively, the
            status and flags returned by the ``get_status`` calls of
            each of this F-Engine's blocks.
        """
        stats = {}
        flags = {}
        if not self.fpga.is_programmed():
            stats['fpga'], flags['fpga'] = self.blocks['fpga'].get_status()
        else:
            for blockname, block in self.blocks.items():
                stats[blockname], flags[blockname] = block.get_status()
        return stats, flags

    def print_status_all(self, use_color=True, ignore_ok=False):
        """
        Print the status returned by ``get_status`` for all blocks in the system.
        If the FPGA is not programmed with F-engine firmware, will only
        print basic FPGA status.

        :param use_color: If True, highlight values with colors based on
            error codes.
        :type use_color: bool

        :param ignore_ok: If True, only print status values which are outside the
           normal range.
        :type ignore_ok: bool

        """
        if not self.fpga.is_programmed():
            print('FPGA stats (not programmed with F-engine image):')
            self.blocks['fpga'].print_status()
        else:
            for blockname, block in self.blocks.items():
                print('Block %s stats:' % blockname)
                block.print_status(use_color=use_color, ignore_ok=ignore_ok)

    def set_equalization(self, eq_start_chan=100, eq_stop_chan=400, 
            start_chan=50, stop_chan=450, filter_ksize=21, target_rms=0.2):
        """
        Set the equalization coefficients to realize a target RMS.

        :param eq_start_chan: Frequency channels below ``eq_start_chan`` will be given the same EQ coefficient
            as ``eq_start_chan``.
        :type eq_start_chan: int

        :param eq_stop_chan: Frequency channels above ``eq_stop_chan`` will be given the same EQ coefficient
            as ``eq_stop_chan``.
        :type eq_stop_chan: int

        :param start_chan: Frequency channels below ``start_chan`` will be given zero EQ coefficients.
        :type start_chan: int

        :param stop_chan: Frequency channels above ``stop_chan`` will be given zero EQ coefficients.
        :type stop_chan: int

        :param filter_ksize: Filter kernel size, for rudimentary RFI removal. This should be an odd value.
        :type filter_ksize: int

        :param target_rms: The target post-EQ RMS. This is normalized such that 1.0 is the saturation level.
            I.e., an RMS of 0.125 means that the RMS is one LSB of a 4-bit signed signal, or 16 LSBs of an
            8-bit signed signal
        :type target_rms: float

        """
        n_cores = self.autocorr.n_signals // self.autocorr.n_signals_per_block
        for i in range(n_cores):
            spectra = self.autocorr.get_new_spectra(i, filter_ksize=filter_ksize)
            n_signals, n_chans = spectra.shape
            coeff_repeat_factor = n_chans // self.eq.n_coeffs
            for j in range(n_signals):
                stream_id = i*n_signals + j
                self.logger.info("Trying to EQ input %d" % stream_id)
                pre_quant_rms = np.sqrt(spectra[j] / 2) # RMS of each real / imag component making up spectra
                eq_coeff, eq_bp = self.eq.get_coeffs(stream_id)
                eq_scale = eq_coeff / (2**eq_bp)
                eq_scale = eq_scale.repeat(coeff_repeat_factor)
                curr_rms = pre_quant_rms * eq_scale
                diff = target_rms / curr_rms
                new_eq = eq_scale * diff
                # stretch the edge coefficients outside the pass band to avoid them heading to infinity
                new_eq[0:eq_start_chan] = new_eq[eq_start_chan]
                new_eq[eq_stop_chan:] = new_eq[eq_stop_chan]
                new_eq[0:start_chan] = 0
                new_eq[stop_chan:] = 0
                self.eq.set_coeffs(stream_id, new_eq[::coeff_repeat_factor])

    def program(self, fpgfile=None):
        """
        Program an .fpg file to an F-engine FPGA.

        :param fpgfile: The .fpg file to be loaded. Should be a path to a
            valid .fpg file. If None is given, the .fpg path provided
            at FEngine instantiation-time will be loaded.
        :type fpgfile: str

        """
        if fpgfile is None:
            fpgfile = self.fpgfile
        else:
            self.fpgfile = fpgfile

        if fpgfile and not isinstance(fpgfile, str):
            raise TypeError("wrong type for fpgfile")

        if fpgfile and not os.path.exists(fpgfile):
            raise RuntimeError("Path %s doesn't exist" % fpgfile)

        try:
            self.logger.info("Loading firmware %s to %s" % (fpgfile, self.hostname))
            self._cfpga.upload_to_ram_and_program(fpgfile)
        except:
            self.logger.exception("Exception when loading new firmware")
            raise RuntimeError("Error during load")
        try:
            self._initialize_blocks()
        except:
            self.logger.exception("Exception when reinitializing firmware blocks")
            raise RuntimeError("Error reinitializing blocks")

    def cold_start_from_config(self, config_file,
                    program=True, initialize=True, test_vectors=False,
                    sync=True, sw_sync=False, enable_eth=True):
        """
        Completely configure an F-engine from scratch, using a configuration
        YAML file.

        :param program: If True, start by programming the SNAP2 FPGA from
            the image currently in flash. Also train the ADC-> FPGA links
            and initialize all firmware blocks.
        :type program: bool

        :param initialize: If True, put all firmware blocks in their default
            initial state. Initialization is always performed if the FPGA
            has been reprogrammed.
        :type initialize: bool

        :param test_vectors: If True, put the F-engine in "frequency ramp" test mode.
        :type test_vectors: bool

        :param sync: If True, synchronize (i.e., reset) the DSP pipeline.
        :type sync: bool

        :param sw_sync: If True, issue a software reset trigger, rather than waiting
            for an external reset pulse to be received over SMA.
        :type sw_sync: bool

        :param enable_eth: If True, enable F-Engine Ethernet output.
        :type enable_eth: bool

        :param config_file: Path to a configuration YAML file.
        :type config_file: str

        """
        import yaml
        self.logger.info("Trying to configure output with config file %s" % config_file)
        if not os.path.exists(config_file):
            self.logger.error("Output configuration file %s doesn't exist!" % config_file)
            raise RuntimeError
        try:
            with open(config_file, 'r') as fh:
                conf = yaml.load(fh, Loader=yaml.CSafeLoader)
            if 'fengines' not in conf:
                self.logger.error("No 'fengines' key in output configuration!")
                raise RuntimeError('Config file missing "fengines" key')
            if 'xengines' not in conf:
                self.logger.error("No 'xengines' key in output configuration!")
                raise RuntimeError('Config file missing "xengines" key')
            chans_per_packet = conf['fengines']['chans_per_packet']
            localboard = conf['fengines'].get(self.hostname, None)
            if localboard is None:
                self.logger.exception("No configuration for F-engine host %s" % self.hostname)
                raise
            localconf = localboard.get(self.pipeline_id, None)
            if localconf is None:
                self.logger.exception("No configuration for pipeline %d found" % self.pipeline_id)
                raise
            first_input_index = localconf['inputs'][0]
            ninput = localconf['inputs'][1] - first_input_index
            macs = conf['xengines']['arp']
            source_ips = localconf['gbes']
            source_port = localconf['source_port']

            dests = []
            for xeng, chans in conf['xengines']['chans'].items():
                dest_ip = xeng.split('-')[0]
                dest_port = int(xeng.split('-')[1])
                start_chan = chans[0]
                nchan = chans[1] - start_chan
                dests += [{'ip':dest_ip, 'port':dest_port, 'start_chan':start_chan, 'nchan':nchan}]
        except:
            self.logger.exception("Failed to parse output configuration file %s" % config_file)
            raise

        self.cold_start(
            program = program,
            initialize = initialize,
            test_vectors = test_vectors,
            sync = sync,
            sw_sync = sw_sync,
            enable_eth = enable_eth,
            chans_per_packet = chans_per_packet,
            first_input_index = first_input_index,
            ninput = ninput,
            macs = macs,
            source_ips = source_ips,
            source_port = source_port,
            dests = dests,
            )


    def cold_start(self, program=True, initialize=True, test_vectors=False,
                   sync=True, sw_sync=False, enable_eth=True,
                   chans_per_packet=32, first_input_index=0, ninput=NIFS,
                   macs={}, source_ips=['10.41.0.101'], source_port=10000,
                   dests=[]):
        """
        Completely configure an F-engine from scratch.

        :param program: If True, start by programming the SNAP2 FPGA from
            the image currently in flash. Also train the ADC-> FPGA links
            and initialize all firmware blocks.
        :type program: bool

        :param initialize: If True, put all firmware blocks in their default
            initial state. Initialization is always performed if the FPGA
            has been reprogrammed, but can be run without reprogramming
            to (quickly) reset the firmware to a known state. Initialization
            does not include ADC->FPGA link training.
        :type initialize: bool

        :param test_vectors: If True, put the F-engine in "frequency ramp" test mode.
        :type test_vectors: bool

        :param sync: If True, synchronize (i.e., reset) the DSP pipeline.
        :type sync: bool

        :param sw_sync: If True, issue a software reset trigger, rather than waiting
            for an external reset pulse to be received over SMA.
        :type sw_sync: bool

        :param enable_eth: If True, enable 40G F-Engine Ethernet output.
        :type enable_eth: bool

        :param chans_per_packet: Number of frequency channels in each output F-engine
            packet
        :type chans_per_packet: int

        :param first_input_index: Zero-indexed ID of the first input connected to this
            board.
        :type first_input_index: int

        :param ninput: Number of inputs to be sent. Values of ``n*32`` may be used
            to spoof F-engine packets from multiple SNAP2 boards.
        :type ninput: int

        :param macs: Dictionary, keyed by dotted-quad string IP addresses, containing
            MAC addresses for F-engine packet destinations. I.e., IP/MAC pairs for all
            X-engines.
        :type macs: dict

        :param source_ips: The IP addresses from which this board should send packets.
            A list with one IP entry per interface.
        :type source_ips: list of str

        :param source_port: The source UDP port from which F-engine packets should be sent.
        :type source_port: int

        :param dests: List of dictionaries describing where packets should be sent. Each
            list entry should have the following keys:

              - 'ip' : The destination IP (as a dotted-quad string) to which packets
                should be sent.
              - 'port' : The destination UDP port to which packets should be sent.
              - 'start_chan' : The first frequency channel number which should be sent
                to this IP / port. ``start_chan`` should be an integer multiple of 16.
              - 'nchans' : The number of channels which should be sent to this IP / port.
                ``nchans`` should be a multiple of ``chans_per_packet``.
        :type dests: List of dict

        """
        if program:
            self.program()
        
        if program or initialize:
            self.initialize(read_only=False)
            self.logger.info('Updating telescope time')
            self.sync.update_internal_time()

        if test_vectors:
            self.logger.info('Enabling EQ TVGs...')
            self.eqtvg.write_freq_ramp()
            self.eqtvg.tvg_enable()
        else:
            self.logger.info('Disabling EQ TVGs...')
            self.eqtvg.tvg_disable()

        if sync:
            self.logger.info("Arming sync generators")
            for eth in self.eths:
                eth.disable_tx()
            self.sync.arm_sync()
            self.sync.arm_noise()
            if sw_sync:
                self.logger.info("Issuing software sync")
                self.sync.sw_sync()

        for sn, source_ip in enumerate(source_ips):
            if sn >= self.neths:
                self.logger.warning('Skipping setting Ethernet interface %d of %d' % (sn, self.neths))
                continue
            mac = MAC_BASE + int(source_ip.split('.')[-1])
            self.eths[sn].configure_source(mac, source_ip, source_port)

        # Configure ARP cache
        for eth in self.eths:
            for ip, mac in macs.items():
                eth.add_arp_entry(ip, mac)

        # Configure packetizer
        channels_to_send = 0
        for dest in dests:
            channels_to_send += dest['nchan']

        pkt_starts, pkt_payloads, word_indices, antchans = self.packetizer.get_packet_info(chans_per_packet, channels_to_send, ninput)
        n_pkts = len(pkt_starts)
        antchan_indices = np.arange(n_pkts*chans_per_packet, dtype=int)[::chans_per_packet]
        chan_indices = antchan_indices % channels_to_send
        ant_indices = antchan_indices // channels_to_send

        # Reorder channels / antennas so they fall in the places we want
        # Current map
        ant_order, chan_order = self.chanreorder.get_antchan_order(raw=False)
        # Start with whatever map is currently loaded. As long
        # as we are double buffering, entries of the map are all independent.
        # (If we are reordering in place every map entry must appear exactly once.
        ant_order = np.array(ant_order, dtype=int)
        chan_order = np.array(chan_order, dtype=int)

        ips = ['0.0.0.0' for _ in range(n_pkts)]
        ports = [0 for _ in range(n_pkts)]
        pkt_num = 0
        ok = True
        for dn, dest in enumerate(dests):
            try:
                start_chan = dest['start_chan']
                nchan = dest['nchan']
                assert nchan % chans_per_packet == 0, "Can't send %d chans with %d-chan packets" % (nchan, chans_per_packet)
                chans = range(start_chan, start_chan + nchan)
                dest_ip = dest['ip']
                if dest_ip not in macs:
                   self.logger.critical("MAC address for IP %s is not known" % dest_ip)
                dest_port = dest['port']
                # loop over packets to this destination, antenna slowest, chan fastest
                for ant in range(ninput):
                    for cn, chan in enumerate(chans[::chans_per_packet]):
                        ips[pkt_num] = dest_ip
                        ports[pkt_num] = dest_port
                        # Use the order maps to figure out where we should put these antchans
                        ant_order[antchans[pkt_num]] = ant
                        chan_order[antchans[pkt_num]] = chans[cn*chans_per_packet:(cn+1)*chans_per_packet]
                        pkt_num += 1
            except:
                self.logger.exception("Failed to parse destination %s" % dest)
                ok = False
                raise

        if ok:
            self.chanreorder.set_antchan_order(ant_order, chan_order)
            self.packetizer.write_config(
                    pkt_starts,
                    pkt_payloads,
                    chan_indices.tolist(),
                    ant_indices.tolist(),
                    ips,
                    ports,
                    [chans_per_packet]*n_pkts,
                    )
        else:
            self.logger.error("Not configuring Ethernet output because configuration builder failed")

        if enable_eth:
            self.logger.info("Enabling Ethernet output")
            for eth in self.eths:
                eth.enable_tx()
        else:
            self.logger.info("Disabling Ethernet output")
            for eth in self.eths:
                eth.disable_tx()

        self.logger.info("Startup of %s complete" % self.hostname)
