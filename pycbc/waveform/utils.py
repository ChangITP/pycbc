# Copyright (C) 2013  Alex Nitz
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.


#
# =============================================================================
#
#                                   Preamble
#
# =============================================================================
#
"""This module contains convenience utilities for manipulating waveforms
"""
from __future__ import absolute_import
from pycbc.types import TimeSeries, FrequencySeries, Array, float32, float64, complex_same_precision_as, real_same_precision_as
import lal
import lalsimulation as sim
from math import frexp
import numpy
import copy
from pycbc.opt import omp_libs, omp_flags
from pycbc import WEAVE_FLAGS
from pycbc.weave import inline

def ceilpow2(n):
    """convenience function to determine a power-of-2 upper frequency limit"""
    signif,exponent = frexp(n)
    if (signif < 0):
        return 1;
    if (signif == 0.5):
        exponent -= 1;
    return (1) << exponent;

def coalign_waveforms(h1, h2, psd=None,
                      low_frequency_cutoff=None,
                      high_frequency_cutoff=None,
                      resize=True):
    """ Return two time series which are aligned in time and phase.

    The alignment is only to the nearest sample point and all changes to the
    phase are made to the first input waveform. Waveforms should not be split
    accross the vector boundary. If it is, please use roll or cyclic time shift
    to ensure that the entire signal is contiguous in the time series.

    Parameters
    ----------
    h1: pycbc.types.TimeSeries
        The first waveform to align.
    h2: pycbc.types.TimeSeries
        The second waveform to align.
    psd: {None, pycbc.types.FrequencySeries}
        A psd to weight the alignment
    low_frequency_cutoff: {None, float}
        The low frequency cutoff to weight the matching in Hz.
    high_frequency_cutoff: {None, float}
        The high frequency cutoff to weight the matching in Hz.
    resize: Optional, {True, boolean}
        If true, the vectors will be resized to match each other. If false,
        they must be the same length and even in length

    Returns
    -------
    h1: pycbc.types.TimeSeries
        The shifted waveform to align with h2
    h2: pycbc.type.TimeSeries
        The resized (if necessary) waveform to align with h1.
    """
    from pycbc.filter import matched_filter
    mlen = ceilpow2(max(len(h1), len(h2)))

    h1 = h1.copy()
    h2 = h2.copy()

    if resize:
        h1.resize(mlen)
        h2.resize(mlen)
    elif len(h1) != len(h2) or len(h2) % 2 != 0:
        raise ValueError("Time series must be the same size and even if you do "
                         "not allow resizing")

    snr = matched_filter(h1, h2, psd=psd,
                         low_frequency_cutoff=low_frequency_cutoff,
                         high_frequency_cutoff=high_frequency_cutoff)

    _, l =  snr.abs_max_loc()
    rotation =  snr[l] / abs(snr[l])
    h1 = (h1.to_frequencyseries() * rotation).to_timeseries()
    h1.roll(l)

    h1 = TimeSeries(h1, delta_t=h2.delta_t, epoch=h2.start_time)
    return h1, h2

def phase_from_frequencyseries(htilde, remove_start_phase=True):
    """Returns the phase from the given frequency-domain waveform. This assumes
    that the waveform has been sampled finely enough that the phase cannot
    change by more than pi radians between each step.

    Parameters
    ----------
    htilde : FrequencySeries
        The waveform to get the phase for; must be a complex frequency series.
    remove_start_phase : {True, bool}
        Subtract the initial phase before returning.

    Returns
    -------
    FrequencySeries
        The phase of the waveform as a function of frequency.
    """
    p = numpy.unwrap(numpy.angle(htilde.data)).astype(
            real_same_precision_as(htilde))
    if remove_start_phase:
        p += -p[0]    
    return FrequencySeries(p, delta_f=htilde.delta_f, epoch=htilde.epoch,
        copy=False)

def amplitude_from_frequencyseries(htilde):
    """Returns the amplitude of the given frequency-domain waveform as a
    FrequencySeries.

    Parameters
    ----------
    htilde : FrequencySeries
        The waveform to get the amplitude of.

    Returns
    -------
    FrequencySeries
        The amplitude of the waveform as a function of frequency.
    """
    amp = abs(htilde.data).astype(real_same_precision_as(htilde))
    return FrequencySeries(amp, delta_f=htilde.delta_f, epoch=htilde.epoch,
        copy=False)

def time_from_frequencyseries(htilde, sample_frequencies=None,
        discont_threshold=0.99*numpy.pi):
    """Computes time as a function of frequency from the given
    frequency-domain waveform. This assumes the stationary phase
    approximation. Any frequencies lower than the first non-zero value in
    htilde are assigned the time at the first non-zero value. Times for any
    frequencies above the next-to-last non-zero value in htilde will be
    assigned the time of the next-to-last non-zero value.

    .. note::
        Some waveform models (e.g., `SEOBNRv2_ROM_DoubleSpin`) can have
        discontinuities in the phase towards the end of the waveform due to
        numerical error. We therefore exclude any points that occur after a
        discontinuity in the phase, as the time estimate becomes untrustworthy
        beyond that point. What determines a discontinuity in the phase is set
        by the `discont_threshold`. To turn this feature off, just set
        `discont_threshold` to a value larger than pi (due to the unwrapping
        of the phase, no two points can have a difference > pi).

    Parameters
    ----------
    htilde : FrequencySeries
        The waveform to get the time evolution of; must be complex.
    sample_frequencies : {None, array}
        The frequencies at which the waveform is sampled. If None, will
        retrieve from ``htilde.sample_frequencies``.
    discont_threshold : {0.99*pi, float}
        If the difference in the phase changes by more than this threshold,
        it is considered to be a discontinuity. Default is 0.99*pi.

    Returns
    -------
    FrequencySeries
        The time evolution of the waveform as a function of frequency.
    """
    if sample_frequencies is None:
        sample_frequencies = htilde.sample_frequencies.numpy()
    phase = phase_from_frequencyseries(htilde).data
    dphi = numpy.diff(phase)
    time = -dphi / (2.*numpy.pi*numpy.diff(sample_frequencies))
    nzidx = numpy.nonzero(abs(htilde.data))[0]
    kmin, kmax = nzidx[0], nzidx[-2]
    # exclude everything after a discontinuity
    discont_idx = numpy.where(abs(dphi[kmin:]) >= discont_threshold)[0]
    if discont_idx.size != 0:
        kmax = min(kmax, kmin + discont_idx[0]-1)
    time[:kmin] = time[kmin]
    time[kmax:] = time[kmax]
    return FrequencySeries(time.astype(real_same_precision_as(htilde)),
        delta_f=htilde.delta_f, epoch=htilde.epoch,
        copy=False)

def phase_from_polarizations(h_plus, h_cross, remove_start_phase=True):
    """Return gravitational wave phase

    Return the gravitation-wave phase from the h_plus and h_cross 
    polarizations of the waveform. The returned phase is always
    positive and increasing with an initial phase of 0.

    Parameters
    ----------
    h_plus : TimeSeries
        An PyCBC TmeSeries vector that contains the plus polarization of the
        gravitational waveform.
    h_cross : TimeSeries
        A PyCBC TmeSeries vector that contains the cross polarization of the
        gravitational waveform.

    Returns
    -------
    GWPhase : TimeSeries
        A TimeSeries containing the gravitational wave phase.

    Examples
    --------s
    >>> from pycbc.waveform import get_td_waveform, phase_from_polarizations
    >>> hp, hc = get_td_waveform(approximant="TaylorT4", mass1=10, mass2=10, 
                         f_lower=30, delta_t=1.0/4096)
    >>> phase = phase_from_polarizations(hp, hc)

    """
    p = numpy.unwrap(numpy.arctan2(h_cross.data, h_plus.data)).astype(
        real_same_precision_as(h_plus))
    if remove_start_phase:
        p += -p[0]    
    return TimeSeries(p, delta_t=h_plus.delta_t, epoch=h_plus.start_time,
        copy=False)

def amplitude_from_polarizations(h_plus, h_cross):
    """Return gravitational wave amplitude

    Return the gravitation-wave amplitude from the h_plus and h_cross 
    polarizations of the waveform. 

    Parameters
    ----------
    h_plus : TimeSeries
        An PyCBC TmeSeries vector that contains the plus polarization of the
        gravitational waveform.
    h_cross : TimeSeries
        A PyCBC TmeSeries vector that contains the cross polarization of the
        gravitational waveform.

    Returns
    -------
    GWAmplitude : TimeSeries
        A TimeSeries containing the gravitational wave amplitude.

    Examples
    --------
    >>> from pycbc.waveform import get_td_waveform, phase_from_polarizations
    >>> hp, hc = get_td_waveform(approximant="TaylorT4", mass1=10, mass2=10, 
                         f_lower=30, delta_t=1.0/4096)
    >>> amp = amplitude_from_polarizations(hp, hc)

    """
    amp = (h_plus.squared_norm() + h_cross.squared_norm()) ** (0.5)
    return TimeSeries(amp, delta_t=h_plus.delta_t, epoch=h_plus.start_time)

def frequency_from_polarizations(h_plus, h_cross):
    """Return gravitational wave frequency

    Return the gravitation-wave frequency as a function of time
    from the h_plus and h_cross polarizations of the waveform. 
    It is 1 bin shorter than the input vectors and the sample times
    are advanced half a bin.

    Parameters
    ----------
    h_plus : TimeSeries
        A PyCBC TimeSeries vector that contains the plus polarization of the
        gravitational waveform.
    h_cross : TimeSeries
        A PyCBC TimeSeries vector that contains the cross polarization of the
        gravitational waveform.

    Returns
    -------
    GWFrequency : TimeSeries
        A TimeSeries containing the gravitational wave frequency as a function
        of time. 

    Examples
    --------
    >>> from pycbc.waveform import get_td_waveform, phase_from_polarizations
    >>> hp, hc = get_td_waveform(approximant="TaylorT4", mass1=10, mass2=10, 
                         f_lower=30, delta_t=1.0/4096)
    >>> freq = frequency_from_polarizations(hp, hc)

    """
    phase = phase_from_polarizations(h_plus, h_cross)
    freq = numpy.diff(phase) / ( 2 * lal.PI * phase.delta_t )
    start_time = phase.start_time + phase.delta_t / 2
    return TimeSeries(freq.astype(real_same_precision_as(h_plus)),
        delta_t=phase.delta_t, epoch=start_time)

# map between tapering string in sim_inspiral table or inspiral 
# code option and lalsimulation constants
taper_map = {
    'TAPER_NONE'    : None,
    'TAPER_START'   : sim.SIM_INSPIRAL_TAPER_START,
    'start'         : sim.SIM_INSPIRAL_TAPER_START,
    'TAPER_END'     : sim.SIM_INSPIRAL_TAPER_END,
    'end'           : sim.SIM_INSPIRAL_TAPER_END,
    'TAPER_STARTEND': sim.SIM_INSPIRAL_TAPER_STARTEND,
    'startend'      : sim.SIM_INSPIRAL_TAPER_STARTEND
}

taper_func_map = {
    numpy.dtype(float32): sim.SimInspiralREAL4WaveTaper,
    numpy.dtype(float64): sim.SimInspiralREAL8WaveTaper
}

def taper_timeseries(tsdata, tapermethod=None, return_lal=False):
    """
    Taper either or both ends of a time series using wrapped 
    LALSimulation functions

    Parameters
    ----------
    tsdata : TimeSeries
        Series to be tapered, dtype must be either float32 or float64
    tapermethod : string
        Should be one of ('TAPER_NONE', 'TAPER_START', 'TAPER_END',
        'TAPER_STARTEND', 'start', 'end', 'startend') - NB 'TAPER_NONE' will
        not change the series!
    return_lal : Boolean
        If True, return a wrapped LAL time series object, else return a 
        PyCBC time series.
    """
    if tapermethod is None:
        raise ValueError("Must specify a tapering method (function was called"
                         "with tapermethod=None)")
    if tapermethod not in taper_map.keys():
        raise ValueError("Unknown tapering method %s, valid methods are %s" % \
                         (tapermethod, ", ".join(taper_map.keys())))
    if not tsdata.dtype in (float32, float64):
        raise TypeError("Strain dtype must be float32 or float64, not "
                    + str(tsdata.dtype))
    taper_func = taper_func_map[tsdata.dtype]
    # make a LAL TimeSeries to pass to the LALSim function
    ts_lal = tsdata.astype(tsdata.dtype).lal()
    if taper_map[tapermethod] is not None:
        taper_func(ts_lal.data, taper_map[tapermethod])
    if return_lal == True:
        return ts_lal
    else:
        return TimeSeries(ts_lal.data.data[:], delta_t=ts_lal.deltaT,
                          epoch=ts_lal.epoch)

_apply_shift_code = r"""
    #include <math.h>
    // cast the output to a float array for faster processing
    // this takes advantage of the fact that complex arrays store
    // their real and imaginary values next to each other in memory
    double* outptr = (double*) out;
    outptr += 2*kmin; // move to the start position
    double cphi = (double) phi;
    double re_h, im_h;
    int update_interval = 100;
    int jj = update_interval;
    double re_shift, im_shift, re_lastshift, im_lastshift;
    double re_inc = cos(cphi);
    double im_inc = sin(cphi);
    for (int kk=kmin; kk<kmax; kk++){
        if (jj == update_interval) {
            // recompute the added value to reduce numerical error
            re_shift = cos(cphi * (double) kk);
            im_shift = sin(cphi * (double) kk);
            jj = 0;
        }
        re_h = *outptr;
        im_h = *(outptr+1);
        *outptr = re_shift * re_h - im_shift * im_h; // the real part
        *(outptr+1) = re_shift * im_h + im_shift * re_h; // the imag part
        // increase the shift for the next element
        re_lastshift = re_shift;
        im_lastshift = im_shift;
        re_shift = re_lastshift * re_inc - im_lastshift * im_inc;
        im_shift = re_lastshift * im_inc + im_lastshift * re_inc;
        jj += 1;
        outptr += 2; // move to the next element
    }
    """
# for single precision
_apply_shift_code32 = _apply_shift_code.replace('double', 'float')

def apply_fseries_time_shift(htilde, dt, kmin=0, copy=True):
    """Shifts a frequency domain waveform in time. The waveform is assumed to
    be sampled at equal frequency intervals.
    """
    out = numpy.array(htilde.data, copy=copy)
    phi = -2 * numpy.pi * dt * htilde.delta_f # pylint:disable=unused-variable
    kmax = len(htilde) # pylint:disable=unused-variable
    if htilde.precision == 'single':
        code = _apply_shift_code32
    else:
        code = _apply_shift_code
    inline(code, ['out', 'phi', 'kmin', 'kmax'],
           extra_compile_args=[WEAVE_FLAGS]+omp_flags,
           libraries=omp_libs)
    if copy:
        htilde = FrequencySeries(out, delta_f=htilde.delta_f, epoch=htilde.epoch,
                                 copy=False)
    return htilde

def apply_fd_time_shift(htilde, shifttime, kmin=0, fseries=None, copy=True):
    """Shifts a frequency domain waveform in time. The shift applied is
    shiftime - htilde.epoch.

    Parameters
    ----------
    htilde : FrequencySeries
        The waveform frequency series.
    shifttime : float
        The time to shift the frequency series to.
    kmin : {0, int}
        The starting index of htilde to apply the time shift. Default is 0.
    fseries : {None, numpy array}
        The frequencies of each element in htilde. This is only needed if htilde is not
        sampled at equal frequency steps.
    copy : {True, bool}
        Make a copy of htilde before applying the time shift. If False, the time
        shift will be applied to htilde's data.

    Returns
    -------
    FrequencySeries
        A frequency series with the waveform shifted to the new time. If makecopy
        is True, will be a new frequency series; if makecopy is False, will be
        the same as htilde.
    """
    dt = float(shifttime - htilde.epoch)
    if dt == 0.:
        # no shift to apply, just copy if desired
        if copy:
            htilde = 1. * htilde
    elif isinstance(htilde, FrequencySeries):
        # FrequencySeries means equally sampled in frequency, use faster shifting
        htilde = apply_fseries_time_shift(htilde, dt, kmin=kmin, copy=copy)
    else:
        if fseries is None:
            fseries = htilde.sample_frequencies.numpy()
        shift = Array(numpy.exp(-2j*numpy.pi*dt*fseries), 
                    dtype=complex_same_precision_as(htilde))
        if copy:
            htilde = 1. * htilde
        htilde *= shift
    return htilde
