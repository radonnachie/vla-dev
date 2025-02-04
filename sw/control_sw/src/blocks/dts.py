import time
import struct
import numpy as np

from .block import Block
from cosmic_f.error_levels import *

LANE_MAP = [0, 1, 10, 11, 2, 3, 8, 9, 4, 5, 6, 7]
_LOCK_TIMEOUT_SECS = 1

class Dts(Block):
    _REG_ADDRESS_CR = 0x0 # Control Register
    _REG_ADDRESS_PC = 0x1 # Parity count
    _REG_ADDRESS_SC = 0x2 # Scramble Code
    _REG_ADDRESS_MD = 0x3 # Meta Data
    _REG_ADDRESS_TM = 0x4 # Timing Register

    _WB_ADDR_META = 6
    def __init__(self, fpga, name, nlanes=12, logger=None):
        super(Dts, self).__init__(fpga, name, logger)
        self.fpga = fpga
        self.nlanes = nlanes

    def _read_reg(self, regoffset=0):
        return self.read_uint('dts', word_offset=regoffset)

    def _write_reg(self, val, regoffset):
        self.write_int('dts', val, word_offset=regoffset)

    def _change_ctrl_reg_bits(self, val, offset, nbits):
        self.change_reg_bits('dts', val, offset, nbits)

    def _set_addr(self, addr):
        self._change_ctrl_reg_bits(addr, 8, 8)

    def _set_data(self, data):
        self._change_ctrl_reg_bits(data, 0, 8)

    def _set_read_strobe(self, v):
        self._change_ctrl_reg_bits(v, 17, 1)

    def _set_write_strobe(self, v):
        self._change_ctrl_reg_bits(v, 16, 1)

    def _toggle_write_strobe(self):
        self._set_write_strobe(0)
        self._set_write_strobe(1)
        self._set_write_strobe(0)

    def _toggle_read_strobe(self):
        self._set_read_strobe(0)
        self._set_read_strobe(1)
        self._set_read_strobe(0)

    def set_reset(self, val):
        self.write_int('rst', val)

    def reset_stats(self):
        self.write_int('stats_rst', 1)
        self.write_int('stats_rst', 0)

    def reset(self):
        self.set_reset(0)
        time.sleep(0.01)
        self.set_reset(1)
        time.sleep(0.01)
        self.set_reset(0)
        time.sleep(0.1)
        self.reset_stats()

    def get_pps_interval(self):
        return self.read_uint('stats_pps_interval')

    def get_pp10s_interval(self):
        return self.read_uint('stats_ten_sec_interval')

    def get_pps_count(self):
        return self.read_uint('stats_pps_count')

    def get_index_interval(self):
        return self.read_uint('stats_index_interval')

    def get_index_count(self):
        return self.read_uint('stats_index_count')

    def get_pp10s_count(self):
        return self.read_uint('stats_ten_sec_count')

    def mute(self):
        self._change_ctrl_reg_bits(0, 18, 1)

    def unmute(self):
        self._change_ctrl_reg_bits(1, 18, 1)

    def _set_cs(self, chip):
        if chip is None:
            self._change_ctrl_reg_bits(0, 19, 12)
        else:
            self._change_ctrl_reg_bits(1<<chip, 19, 12)

    def _read_data(self):
        return self._read_reg(self._WB_ADDR_META) & 0xff

    def get_lock_state(self):
        return (self._read_reg(self._WB_ADDR_META) >> 8) & (2**self.nlanes - 1)

    def get_gty_lock_state(self):
        return (self._read_reg(self._WB_ADDR_META) >> (8+self.nlanes)) & (2**self.nlanes - 1)

    def advance_stream(self, stream):
        v = (1<<stream)
        self.write_int('dts', 0, word_offset=1)
        self.write_int('dts', v, word_offset=1)
        self.write_int('dts', 0, word_offset=1)

    def delay_stream(self, stream):
        v = (1<<stream)<<12
        self.write_int('dts', 0, word_offset=1)
        self.write_int('dts', v, word_offset=1)
        self.write_int('dts', 0, word_offset=1)

    def reset_delays(self):
        self._write_reg(0, 1)
        self._write_reg(1<<31, 1)
        self._write_reg(0, 1)

    def set_lane_map(self, lanemap):
        x = 0
        for i in range(self.nlanes):
            x += (lanemap[i] << (4*i))
        self._write_reg(x & 0xffffffff, 2)
        self._write_reg(x >> 32, 3)

    def _latch_parity_errs(self):
        for i in range(self.nlanes):
            self._set_cs(None)
            self._set_read_strobe(1)
            self._set_addr(0)
            self._set_data(1<<4)
            self._set_cs(i)
            self._toggle_write_strobe()
        self._set_cs(None)

    def get_parity_errs(self):
        #self._latch_parity_errs() 
        dout = []
        for i in range(self.nlanes):
            dts_dict = {}
            self._set_cs(None)
            self._set_read_strobe(0)
            self._set_addr(self._REG_ADDRESS_PC)
            self._set_cs(i) # causes edge on drb
            dts_dict = {
                'time': 0,
                'acc': 0,
                'count': 0,
            }
            for j in range(3):
                dts_dict['time'] += (self._read_data() << (8*(2-j)))
                self._toggle_read_strobe()
            for j in range(4):
                dts_dict['acc'] += (self._read_data() << (8*(3-j)))
                self._toggle_read_strobe()
            for j in range(2):
                dts_dict['count'] += (self._read_data() << (8*(1-j)))
                self._toggle_read_strobe()
            dout += [dts_dict]
        self._set_cs(None)
        return dout

    def get_meta_data2(self):
        for i in range(self.nlanes):
            print("meta %d:" % i)
            self._set_cs(None)
            self._set_read_strobe(0)
            self._set_addr(self._REG_ADDRESS_MD)
            self._set_cs(i) # causes edge on drb
            dts_dict = {
                'antnum': None,
                'ifnum': None,
                'chipnum': None,
                'is3bit': None,
                'pll_lock0': None,
                'pll_lock1': None,
                'unused': None,
            }
            d = 0
            for j in range(4):
                x = self._read_data()
                self._toggle_read_strobe()
                d += (x << (8*(3-j)))
            print("0x%.8x" %d)
        self._set_cs(None)

    def get_meta_data(self):
        dout = []
        for i in range(self.nlanes):
            self._set_cs(None)
            self._set_read_strobe(0)
            self._set_addr(self._REG_ADDRESS_MD)
            self._set_cs(i) # causes edge on drb
            dts_dict = {
                'antnum': None,
                'ifnum': None,
                'chipnum': None,
                'is3bit': None,
                'pll_lock0': None,
                'pll_lock1': None,
                'unused': None,
            }
            x = self._read_data()
            self._toggle_read_strobe()
            dts_dict['antnum'] = x
            x = self._read_data()
            self._toggle_read_strobe()
            dts_dict['ifnum'] = x >> 4
            dts_dict['chipnum'] = x & 0xf
            x = self._read_data()
            self._toggle_read_strobe()
            dts_dict['is3bit'] = x >> 4
            dts_dict['pll_lock0'] = x & 0xf
            x = self._read_data()
            self._toggle_read_strobe()
            dts_dict['pll_lock1'] = x >> 4
            dts_dict['unused'] = x & 0xf
            dout += [dts_dict]
        self._set_cs(None)
        return dout

    def get_status(self, lanes='all'):
        if lanes == 'all':
            lanes = range(self.nlanes)
        x = self.get_meta_data()
        gty_lock = self.get_gty_lock_state()
        dts_lock = self.get_lock_state()
        stats = {}
        flags = {}
        for i in lanes:
            for k, v in x[i].items():
                lk = "lane%d:%s" % (i,k)
                stats[lk] = v
                if k == "pll_lock0" and v != 0b11:
                    flags[lk] = FENG_ERROR
            lk = "lane%d:gty_lock" % (i)
            stats[lk] = (gty_lock >> i) & 1
            if not stats[lk]:
                flags[lk] = FENG_ERROR
            lk = "lane%d:dts_lock" % (i)
            stats[lk] = (dts_lock >> i) & 1
            if not stats[lk]:
                flags[lk] = FENG_ERROR

        return stats, flags

    def initialize(self, read_only=False):
        if read_only:
            return
        self.reset()
        t0 = time.time()
        locked = self.get_lock_state()
        while(locked != 2**self.nlanes - 1):
            if time.time() - t0 > _LOCK_TIMEOUT_SECS:
                self._warning("Failed to lock after %.1f seconds" % _LOCK_TIMEOUT_SECS)
                break
            time.sleep(0.1)
            locked = self.get_lock_state()
        self._info("Lock state: 0x%.3x" % locked)
        self.set_lane_map(LANE_MAP)
        self.align_lanes()
        self.unmute()

    def get_snapshot_sync(self):
        x = self.fpga.snapshots[self.prefix + 'stats_sync_snapshot'].read(man_trig=True, man_valid=True)
        return x['data']['data']

    def get_snapshot_pps(self):
        x = self.fpga.snapshots[self.prefix + 'stats_pps_snapshot'].read(man_trig=False, man_valid=True)
        return x['data']['data']

    def get_snapshot_index(self):
        x = self.fpga.snapshots[self.prefix + 'stats_index_snapshot'].read(man_trig=False, man_valid=True)
        return x['data']['data']

    def get_pps_oos_count(self):
        return self.read_uint('stats_pps_out_of_sync_count')

    def get_sync_oos_count(self):
        return self.read_uint('stats_sync_out_of_sync_count')

    def get_index_oos_count(self):
        return self.read_uint('stats_index_out_of_sync_count')

    def get_sync_oos_count_per_lane(self):
        rv = []
        for i in range(self.nlanes):
            rv += [self.read_uint('stats_Subsystem_%d_count' % i)] #TODO fix this name!
        return rv

    def get_pp10s_sec_oos_count(self):
        return self.read_uint('stats_ten_sec_out_of_sync_count')

    def print_sync(self, locked=0xfff, mux_factor=4):
        x = self.get_snapshot_sync()[::mux_factor]
        for dn, d in enumerate(x[0:64]):
           print("%.4d" % dn, np.binary_repr(d & locked, width=12))

    def align_lanes(self, mux_factor=4, retries=0):
        locked = self.get_lock_state()
        if locked == 0:
            self._error("Can't align because no lanes are locked!")
            raise RuntimeError
        use_lane = [True] * self.nlanes
        for lane in range(self.nlanes):
            if not ((locked >> lane) & 1):
                use_lane[lane] = False
                self._warning("Not trying to align lane %d because it is not locked" % lane)
        ref_lane = None
        for lane in range(self.nlanes):
            if use_lane[lane]:
                ref_lane = lane
                break
        if ref_lane is None:
            self._error("No reference lane found. This shouldn't happen!")
            raise RuntimeError

        # Function to extract a single bit from the locked vector
        def extract_lane(lane, sync):
            out = [(x >> lane) & 1 for x in sync]
            return out

        # Find reference point
        lanes_sync = self.get_snapshot_sync()[::mux_factor]
        sync_ref = extract_lane(ref_lane, lanes_sync)
        try:
            # Find the second positive edge of the sync, i.e., the sync after the
            # first nosync after the first sync.
            # This gives some room to ignore snapshot edge effects
            first_sync = sync_ref.index(1)
            self._debug("Reference: first sync at %d" % first_sync)
            first_nosync = sync_ref[first_sync+1 :].index(0) + first_sync + 1
            self._debug("Reference: first post-sync at %d" % first_nosync)
            ref_point = sync_ref[first_nosync+1 :].index(1) + first_nosync + 1
            self._debug("Reference: Second (reference) sync at %d" % ref_point)
            self._debug("+/-8 around reference: %s" % (str(lanes_sync[ref_point-8:ref_point+8])))
        except ValueError:
            self._error("Failed to find reference sync point")
            raise
        for lane in range(self.nlanes):
            if not use_lane[lane]:
                self._info("Skipping lane %d because it wasn't locked" % lane)
                continue
            if lane == ref_lane:
                self._info("Lane %d is the sync reference" % lane)
                continue
            sync_lane = extract_lane(lane, lanes_sync)
            # Find first sync at or after the reference
            t_after = sync_lane[ref_point:].index(1)
            # Find first sync at or before the reference
            t_before = sync_lane[:ref_point+1][::-1].index(1)
            if t_after == 0:
                self._info("Lane %d is already in sync" % lane)
                # Already in sync
                continue
            if t_after < t_before:
                self._info("Advancing lane %d by %d steps" % (lane, t_after))
                for i in range(t_after):
                    self.advance_stream(lane)
            else:
                self._info("Delaying lane %d by %d steps" % (lane, t_before))
                for i in range(t_before):
                    self.delay_stream(lane)
        synced = self.is_synced()
        if not synced:
            if retries == 0:
                self._critical("Automatic lane syncing failed!")
            elif retries > 0:
                self._warning("Retrying DTS alignment")
                return self.align_lanes(mux_factor=mux_factor, retries=retries-1)
        return synced

    def is_synced(self):
        synced = self.get_snapshot_sync()
        for v in synced:
            if v != 0 and v != (2**self.nlanes-1):
                return False
        return True

    def set_corruption_control(self, v):
        self.change_reg_bits('dts', v, 0, 12, word_offset=5)

    def get_snapshot_data(self, band):
        ss = self.fpga.snapshots[self.prefix + 'stats_data_ss_snapshot%d' % band]
        x, t = ss.read_raw(man_trig=True, man_valid=True)
        nwords = x['length'] // 2
        # 8-bit DTS data are cast to 12-bits by the interface, and then cast to 16-bits
        # by the snapshot. So shift out 8 unused bits
        d = np.array(struct.unpack('>%dH' % nwords, x['data'])) >> 8
        # Convert offset binary to 2's complement
        d = (d + 128) % 256
        d[d>=128] -= 256
        return d

    def get_bit_stats(self, band):
        d = self.get_snapshot_data(band)
        mean = np.mean(d)
        var = np.var(d)
        return mean, var, np.sqrt(var)

    def get_histogram(self, band):
        from matplotlib import pyplot as plt
        plt.hist(self.get_snapshot_data(band), bins=range(-128,129))
        plt.show()

    def get_snapshot_fft(self, band, oversample_factor=1, use_window=True, start_index=0, decimation=1):
        x = self.get_snapshot_data(band)[start_index::decimation]
        if use_window:
            window = np.sinc(np.linspace(-2*np.pi, 2*np.pi, x.shape[0]))
            x = x*window
        X = np.fft.rfft(x, x.shape[0]*oversample_factor)
        p = np.abs(X)**2
        return p

    def arm_pps(self):
        self.write_int('arm', 0)
        self.write_int('arm', 1)
        self.write_int('arm', 0)

    def get_accumulated_fft(self, band, N, **kwargs):
        p = self.get_snapshot_fft(band, **kwargs)
        for i in range(1, N):
            p += self.get_snapshot_fft(band, **kwargs)
        return p
