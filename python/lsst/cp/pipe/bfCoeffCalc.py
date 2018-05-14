#
# LSST Data Management System
#
# Copyright 2008-2017  AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#

"""Calculation of brighter-fatter effect correlations and kernels."""
from __future__ import print_function

from builtins import zip
from builtins import str
from builtins import range
import os
from scipy import stats
import numpy as np
import matplotlib.pyplot as plt
# following line is not actually unused, it is required for 3d projection
from mpl_toolkits.mplot3d import axes3d   # noqa: F401

import lsstDebug
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.display as afwDisp
from lsst.ip.isr import IsrTask
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase


class BfTaskConfig(pexConfig.Config):
    """Config class for bright-fatter effect coefficient calculation."""

    isr = pexConfig.ConfigurableField(
        target=IsrTask,
        doc="""Task to perform instrumental signature removal or load a post-ISR image; ISR consists of:
            - assemble raw amplifier images into an exposure with image, variance and mask planes
            - perform bias subtraction, flat fielding, etc.
            - mask known bad pixels
            - provide a preliminary WCS
            """,
    )
    doCalcGains = pexConfig.Field(
        dtype=bool,
        doc="Measure the per-amplifier gains using the PTC method",
        default=True,
    )
    maxIterRegression = pexConfig.Field(
        dtype=int,
        doc="Maximum number of iterations for the iterative regression fitter",
        default=10
    )
    nSigmaClipGainCalc = pexConfig.Field(
        dtype=int,
        doc="Number of sigma to clip to during gain calculation",
        default=5
    )
    nSigmaClipRegression = pexConfig.Field(
        dtype=int,
        doc="Number of sigma to clip to during iterative regression",
        default=3
    )
    xcorrCheckRejectLevel = pexConfig.Field(
        dtype=float,
        doc="Sanity check level for the sum of the input cross-correlations. Arrays which "
        "sum to greater than this are discarded before the clipped mean is calculated.",
        default=1.0  # xxx change this back once the problem is fixed!
        # default=0.2
    )
    maxIterSOR = pexConfig.Field(
        dtype=int,
        doc="The maximum number of iterations allowed for the successive over-relaxation method",
        default=10000
    )
    eLevelSOR = pexConfig.Field(
        dtype=float,
        doc="The target residual error for the successive over-relaxation method",
        default=5.0e-14
    )
    nSigmaClipKernelGen = pexConfig.Field(
        dtype=float,
        doc="Number of sigma to clip to during pixel-wise clipping when generating the kernel",
        default=4
    )
    nSigmaClipXCorr = pexConfig.Field(
        dtype=float,
        doc="Number of sigma to clip when calculating means for the cross correlation",
        default=5
    )
    maxLag = pexConfig.Field(
        dtype=int,
        doc="The maximum lag to use when calculating the cross correlation/kernel",
        default=5
    )
    nPixBorderGainCalc = pexConfig.Field(
        dtype=int,
        doc="The number of border pixels to exclude when calculating the gain",
        default=10
    )
    nPixBorderXCorr = pexConfig.Field(
        dtype=int,
        doc="The number of border pixels to exclude when calculating the cross correlation/kernel",
        default=10
    )
    biasCorr = pexConfig.Field(
        dtype=float,
        doc="A scary, empirically determined correction-factor correcting for sigma-clipping" +
        " a non-Gaussian distribution",
        default=0.9241
    )
    backgroundBinSize = pexConfig.Field(
        dtype=int,
        doc="Size of the background bins",
        default=128
    )
    fixPtcThroughOrigin = pexConfig.Field(
        dtype=bool,
        doc="Contrain the fit of the PTC to go through the origin?",
        default=True
    )
    level = pexConfig.ChoiceField(
        doc="The level at which to calculate kernels",
        dtype=str, default="CCD",
        allowed={
            "AMP": "Every amplifier treated separately",
            "CCD": "One kernel per CCD",
        }
    )

    def validateIsrConfig(self, logger):
        """Check that appropriate ISR settings are being used for brighter-fatter kernel calculation."""
        mandatory = ['doAssembleCcd', 'doOverscan']  # raise if False
        forbidden = ['doApplyGains', 'normalizeGains', 'doFlat', 'doFringe', 'doAddDistortionModel',
                     'doBrighterFatter', 'doUseOpticsTransmission', 'doUseFilterTransmission',
                     'doUseSensorTransmission', 'doUseAtmosphereTransmission', 'doGuider', 'doStrayLight',
                     'doTweakFlat']  # raise if True
        desirableTrue = ['doBias', 'doDark', 'doCrosstalk', 'doDefect', 'doLinearize']  # WARN if False

        # How should we handle saturation/bad regions?
        # 'doSaturationInterpolation': True
        # 'doNanInterpAfterFlat': False
        # 'doSaturation': True
        # 'doSuspect': True
        # 'doWidenSaturationTrails': True
        # 'doSetBadRegions': True

        configDict = self.isr.toDict()

        for configParam in mandatory:
            if configDict[configParam] is False:
                raise RuntimeError('Must set config.isr.%s to True '
                                   'for brighter-fatter kernel calulation'%configParam)

        for configParam in forbidden:
            if configDict[configParam] is True:
                raise RuntimeError('Must set config.isr.%s to False '
                                   'for brighter-fatter kernel calulation'%configParam)

        for configParam in desirableTrue:
            if configDict[configParam] is False:
                logger.warn('Found config.isr.%s set to False for brighter-fatter kernel calulation. '
                            'It is probably desirable to have this set to True'%configParam)

        # subtask settings
        if not self.isr.assembleCcd.doTrim:
            raise RuntimeError('Must trim when assembling CCDs. Set config.isr.assembleCcd.doTrim to True')


class BfTaskRunner(pipeBase.TaskRunner):
    """Subclass of TaskRunner for the bfTask.

    This transforms the processed arguments generated by the ArgumentParser into the arguments expected by
    bfTask.run().

    bfTask.run() takes a two arguments, one of which is the dataRef (as usual), and the other is the
    list of visitPairs, in the form of a list of tuples. This list is supplied on the command line as
    documented, and this class parses that, and passes the parsed version to the run() method.

    See pipeBase.TaskRunner for more information.
    """

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        """Parse the visit list and pass through explicitly."""
        visitPairs = parsedCmd.visitPairs

        tuples = visitPairs.split("),(")  # split
        tuples[0] = tuples[0][1:]  # remove leading "(
        tuples[-1] = tuples[-1][:-1]  # remove trailing )"
        visitPairs = [(int(v1), int(v2)) for (v1, v2) in [tup.split(',') for tup in tuples]]  # cast to int

        # if the reviewer is a die-hard regex fan then uncomment the below, but the above is more readable
        # visitPairs = re.findall(r'\((\d+),\s*(\d+)\)', visitPairs)  # break down visit pair list
        # visitPairs = [tuple([int(y) for y in x]) for x in visitPairs]  # make a tuple of ints
        return pipeBase.TaskRunner.getTargetList(parsedCmd, visitPairs=visitPairs, **kwargs)


class BfDataIdContainer(pipeBase.DataIdContainer):
    """A DataIdContainer for the BF task."""

    def makeDataRefList(self, namespace):
        """Compute refList based on idList.

        This method must be defined as the dataset does not exist before this task is run.

        Parameters
        ----------
        namespace
            Results of parsing command-line (with ``butler`` and ``log`` elements).

        Notes
        -----
        Not called if ``add_id_argument`` called with ``doMakeDataRefList=False``.
        Note that this is almost a copy-and-paste of the vanilla implementation, but without checking
        if the datasets already exist as this task exists to make them.
        """
        if self.datasetType is None:
            raise RuntimeError("Must call setDatasetType first")
        butler = namespace.butler
        for dataId in self.idList:
            refList = list(butler.subset(datasetType=self.datasetType, level=self.level, dataId=dataId))
            # exclude nonexistent data
            # this is a recursive test, e.g. for the sake of "raw" data
            if not refList:
                namespace.log.warn("No data found for dataId=%s", dataId)
                continue
            self.refList += refList


class BfTask(pipeBase.CmdLineTask):
    """Bright-fatter effect coefficient calculation task.

    See http://ls.st/ldm-151 Chapter 4, Calibration Products Production for further details
    regarding the inputs and outputs.
    """

    RunnerClass = BfTaskRunner
    ConfigClass = BfTaskConfig
    _DefaultName = "bf"

    def __init__(self, *args, **kwargs):
        """Constructor for the BfTask."""
        pipeBase.CmdLineTask.__init__(self, *args, **kwargs)
        self.makeSubtask("isr")

        self.debug = lsstDebug.Info(__name__)
        if self.debug.enabled:
            self.log.info("Running with debug enabled...")
            # if we're displaying, test it works, and save the displays for later
            # it's worth testing here as displays are flaky and sometimes can't be contacted
            # and given processing takes a while, it's a shame to fail late due to display issues
            if self.debug.display:
                try:
                    afwDisp.setDefaultBackend(self.debug.displayBackend)
                    afwDisp.Display.delAllDisplays()
                    self.disp1 = afwDisp.Display(0, open=True)
                    self.disp2 = afwDisp.Display(1, open=True)

                    im = afwImage.ImageF(1, 1)
                    im.array[:] = [[1]]
                    self.disp1.mtv(im)
                    self.disp1.erase()
                except NameError:
                    self.debug.display = False
                    self.log.warn('Failed to setup/connect to display! Debug display has been disabled')

        plt.interactive(False)  # stop windows popping up when plotting. When headless, use 'agg' backend too
        self.config.validateIsrConfig(self.log)
        self.config.validate()
        self.config.freeze()

    @classmethod
    def _makeArgumentParser(cls):
        """Augment argument parser for the BfTask."""
        parser = pipeBase.ArgumentParser(name=cls._DefaultName)
        parser.add_argument("--visitPairs", help="The list of visit pairs to use, as a list of tuples "
                            "enclosed in quotes e.g. \"(123,456),(789,987),(654,321)\""
                            "NB: must be comma-separated-tuples with no spaces, enclosed in quotes!")
        parser.add_id_argument("--id", datasetType="bfKernelNew", ContainerClass=BfDataIdContainer,
                               help="The ccds to use, e.g. --id ccd=0..100")
        return parser

    @pipeBase.timeMethod
    def run(self, dataRef, visitPairs):
        """Run the brighter-fatter measurement task.

        For a dataRef (which is each ccd here), and given a list of visit pairs, calulate the
        brighter-fatter kernel for the ccd.

        Parameters
        ----------
        dataRef : list of lsst.daf.persistence.ButlerDataRef
            dataRef for the CCD for the visits to be fit.
        visitPairs : `iterable` of `tuple` of `int`
            Pairs of visit numbers to be processed together
        """
        xcorrs = {}  # dict of lists keyed by either amp or CCD depending on level
        means = {}
        kernels = {}

        # setup necessary objects
        ccdNum = dataRef.dataId['ccd']
        if self.config.level == 'CCD':
            xcorrs = {ccdNum: []}
            means = {ccdNum: []}
        elif self.config.level == 'AMP':
            detector = dataRef.get('raw_detector')
            ampInfoCat = detector.getAmpInfoCatalog()
            ampNames = [amp.getName() for amp in ampInfoCat]
            xcorrs = {key: [] for key in ampNames}
            means = {key: [] for key in ampNames}

        # calculate or retrieve the gains
        if self.config.doCalcGains:
            self.log.info('Beginning gain estimation for CCD %s'%ccdNum)
            gains, nomGains = self.estimateGains(dataRef, visitPairs)
            dataRef.put(gains, datasetType='bfGain')
            self.log.info('Finished gain estimation for CCD %s'%ccdNum)
        else:
            gains = dataRef.get('bfGain')
            if not gains:
                self.log.fatal('Failed to retrieved gains for CCD %s'%ccdNum)
                raise RuntimeError("Must either calculate or supply gains for %s"%ccdNum)
            self.log.info('Retrieved stored gain for CCD %s'%ccdNum)
        self.log.debug('CCD %s has gains %s'%(ccdNum, gains))

        # Loop over pairs of visits, calculating the cross correlations at the required level
        for (v1, v2) in visitPairs:

            dataRef.dataId['visit'] = v1
            exp1 = self.isr.runDataRef(dataRef).exposure
            dataRef.dataId['visit'] = v2
            exp2 = self.isr.runDataRef(dataRef).exposure
            del dataRef.dataId['visit']

            self.log.info('Preparring images for cross corellation calculation for CCD %s'%ccdNum)
            # note the shape of these returns depends on level
            _scaledMaskedIm1, _means1 = self._prepareImage(exp1, gains, self.config.level)
            _scaledMaskedIm2, _means2 = self._prepareImage(exp2, gains, self.config.level)

            if self.debug.enabled:
                try:
                    frameId1 = exp1.getMetadata().get("FRAMEID")
                    frameId2 = exp2.getMetadata().get("FRAMEID")
                    frameId = '_diff_'.join(frameId1, frameId2)
                except:
                    frameId = 'Im1 diff Im2'

            # depending on level this is either one pass or n_amps
            for det_object in _scaledMaskedIm1.keys():  # looping over either CCD or amps
                xcorrs[det_object].append(self._crossCorrelate(_scaledMaskedIm1[det_object],
                                                               _scaledMaskedIm2[det_object]))
                means[det_object].append([_means1[det_object], _means2[det_object]])

            # this is awkward now that code is refactored
            # not sure how best to make this work without passing everything around
            # if self.debug.enabled:
            #     rawMeans = [afwMath.makeStatistics(im.getMaskedImage(), afwMath.MEANCLIP).getValue() \
            #                 for im in [exp1, exp2]]
            #     title = "Visits %s; %s, CCDs %s <I> = %.3f (%s) Var = %.4f"%(self._getNameOfSet([v1]),
            #                                                                  self._getNameOfSet([v2]),
            #                                                                  self._getNameOfSet([ccdNum]),
            #                                                                  _means1+rawMeans[1],
            #                                                                  im1.getFilter().getName(),
            #                                                                  float(xcorr[0, 0]) /
            #                                                                       (_means1+rawMeans[1]))
            #     detObjId = str(ccdNum)
            #     if level=='AMP': detObjId += ampName
            #     fileName = (os.path.join(self.debug.debugPlotPath, '_'.join(['xcorr_visit', str(v1),
            #                                                                  str(v2), 'ccd', str(ccdNum)])))
            #     fileName += self.debug.plotType
            #     self._plotXcorr(xcorr.copy(), (xcorrMeans[0]+means[1]),
            #                     title=title, saveToFileName=fileName)

        # generate the kernel(s)
        self.log.info('Generating kernel(s) for %s'%ccdNum)
        for det_object in xcorrs.keys():  # looping over either CCD or amps
            if self.config.level == 'CCD':
                objId = 'CCD %s'%det_object
            elif self.config.level == 'AMP':
                objId = 'CCD %s AMP %s'%(ccdNum, det_object)
            kernels[det_object] = self._generateKernel(xcorrs[det_object], means[det_object], objId)
        # kernels['level'] = self.config.level  # this might be useful, but might also mess things up
        dataRef.put(kernels)

        self.log.info('Finished generating kernel(s) for %s'%ccdNum)
        return pipeBase.Struct(exitStatus=0)

    def _prepareImage(self, exp, gains, level):
        """Prepare images for cross-correlation calculation.

        Each amp has borders applied, is rescaled by the gain, and has the sigma-clipped mean subtracted.
        If the level is 'CCD' then this is done for the whole image in-place so that it can be
        cross-correlated.
        If the level is 'AMP' then this is done per-amplifier, and each amp-image returned.

        Parameters:
        -----------
        exp1 : `lsst.afw.image.exposure.ExposureF`
            The exposure to prepare
        gains : `dict` of `float`
            Dictionary of the amplifier gain values, keyed by amplifier name
        level : `str`
            Either `AMP` or `CCD`

        Returns:
        --------
        scaledMaskedIms : `dict` of `lsst.afw.image.maskedImage.MaskedImageF`
            Depending on level, this is either one item, or n_amp items, keyed by detectorId or ampName

        Notes:
        ------
        This function is controlled by the following pexConfig parameters:
        nPixBorderXCorr : `int`
            The number of border pixels to exclude
        nSigmaClipXCorr : `float`
            The number of sigma to be clipped to
        """
        # TODO: see if you can do away with temp and rescaleTemp
        assert(isinstance(exp, afwImage.ExposureF))

        local_exp = exp.clone()  # we don't want to modify the image passed in
        del exp  # ensure we don't make mistakes!

        border = self.config.nPixBorderXCorr
        sigma = self.config.nSigmaClipXCorr

        sctrl = afwMath.StatisticsControl()
        sctrl.setNumSigmaClip(sigma)

        means = {}
        returnAreas = {}

        detector = local_exp.getDetector()
        ampInfoCat = detector.getAmpInfoCatalog()

        mi = local_exp.getMaskedImage()  # makeStatistics does not seem to take exposures
        temp = mi.clone()

        # Rescale each amp by the appropriate gain and subtract the mean.
        # NB these are views modifying the image in-place
        for amp in ampInfoCat:
            ampName = amp.getName()
            rescaleIm = mi[amp.getBBox()]  # the soon-to-be scaled, mean subtractedm, amp image
            rescaleTemp = temp[amp.getBBox()]
            mean = afwMath.makeStatistics(rescaleIm, afwMath.MEANCLIP, sctrl).getValue()
            gain = gains[ampName]
            rescaleIm *= gain
            rescaleTemp *= gain
            self.log.debug(mean*gain, afwMath.makeStatistics(rescaleIm, afwMath.MEANCLIP, sctrl).getValue())
            rescaleIm -= mean*gain

            if level == 'AMP':  # build the dicts if doing amp-wise
                means[ampName] = afwMath.makeStatistics(rescaleTemp[border:-border, border:-border],
                                                        afwMath.MEANCLIP, sctrl).getValue()
                returnAreas[ampName] = rescaleIm

        if level == 'CCD':   # else just average the whole CCD
            # xxx I think temp can be done away with by just adding the mean here, right?
            detName = local_exp.getDetector().getId()
            means[detName] = afwMath.makeStatistics(temp[border:-border, border:-border],
                                                    afwMath.MEANCLIP, sctrl).getValue()
            returnAreas[detName] = rescaleIm

        return returnAreas, means

    def _crossCorrelate(self, maskedIm0, maskedIm1, frameId=None, detId=None):
        """Calculate the cross-correlation of an area.

        If the areas in question contains many amplifiers then these must have been gain corrected.

        Parameters:
        -----------
        area1 : `lsst.afw.image.MaskedImageF`
            The first image area
        area2 : `lsst.afw.image.MaskedImageF`
            The first image area
        frameId : `str`, optional
            The frame identifier for use in the filename if writing debug outputs.
        detId : `str`, optional
            The detector identifier (CCD, or CCD+amp depending on level)
            for use in the filename if writing debug outputs.

        Returns:
        --------
        xcorr : `np.ndarray`
            The quarter-image cross-corellation
        mean : `float`
            The as-calculated means of the input images (clipped, and with borders applied)

        Notes:
        ------
        This function is controlled by the following pexConfig parameters:
        maxLag : `int`
            The maximum lag to use in the cross-correlation calculation
        nPixBorderXCorr : `int`
            The number of border pixels to exclude
        nSigmaClipXCorr : `float`
            The number of sigma to be clipped to
        biasCorr : `float`
            Parameter used to correct from the bias introduced by the sigma cuts.
        """
        maxLag = self.config.maxLag
        border = self.config.nPixBorderXCorr
        sigma = self.config.nSigmaClipXCorr
        biasCorr = self.config.biasCorr

        sctrl = afwMath.StatisticsControl()
        sctrl.setNumSigmaClip(sigma)

        # Diff the images, and apply border
        diff = maskedIm0.clone()
        diff -= maskedIm1.getImage()
        diff = diff[border:-border, border:-border]

        if self.debug.writeDiffImages:
            filename = '_'.join(['diff', 'CCD', detId, frameId, '.fits'])
            diff.writeFits(os.path.join(self.debug.debugDataPath, filename))

        # Subtract background.  It should be a constant, but it isn't always
        # xxx do we want some logic here for whether to subtract or not? Or a config option?
        binsize = self.config.backgroundBinSize
        nx = diff.getWidth()//binsize
        ny = diff.getHeight()//binsize
        bctrl = afwMath.BackgroundControl(nx, ny, sctrl, afwMath.MEANCLIP)
        bkgd = afwMath.makeBackground(diff, bctrl)
        diff -= bkgd.getImageF(afwMath.Interpolate.CUBIC_SPLINE, afwMath.REDUCE_INTERP_ORDER)

        if self.debug.writeDiffImages:
            filename = '_'.join(['bgSub', 'diff', 'CCD', detId, frameId, '.fits'])
            diff.writeFits(os.path.join(self.debug.debugDataPath, filename))
        if self.debug.display:
            self.disp1.mtv(diff, title=frameId)

        self.log.debug("Median and variance of diff:")
        self.log.debug(afwMath.makeStatistics(diff, afwMath.MEDIAN, sctrl).getValue())
        self.log.debug(afwMath.makeStatistics(diff, afwMath.VARIANCECLIP,
                                              sctrl).getValue(), np.var(diff.getImage().getArray()))

        # Measure the correlations
        dim0 = diff[0: -maxLag, : -maxLag]
        dim0 -= afwMath.makeStatistics(dim0, afwMath.MEANCLIP, sctrl).getValue()
        width, height = dim0.getDimensions()
        xcorr = np.zeros((maxLag + 1, maxLag + 1), dtype=np.float64)

        for xlag in range(maxLag + 1):
            for ylag in range(maxLag + 1):
                dim_xy = diff[xlag:xlag + width, ylag: ylag + height].clone()
                dim_xy -= afwMath.makeStatistics(dim_xy, afwMath.MEANCLIP, sctrl).getValue()
                dim_xy *= dim0
                xcorr[xlag, ylag] = afwMath.makeStatistics(dim_xy,
                                                           afwMath.MEANCLIP, sctrl).getValue()/(biasCorr)

        # xxx to reinstate this debug block need to work out what 'means' was before the refactor
        # and why this is even interesting. Probably just remove the whole thing
        # xcorr_full = self._tileArray(xcorr)
        # self.log.debug(sum(means), xcorr[0, 0], np.sum(xcorr_full), xcorr[0, 0]/sum(means),
        #                np.sum(xcorr_full)/sum(means))
        return xcorr

    def estimateGains(self, dataRef, visitPairs):
        """Estimate the gains of the amplifiers in the CCD using the specified visits.

        Given a dataRef and list of flats of varying intensity, calculate the gain for each
        CCD specified using the PTC method.

        The fixPtcThroughOrigin config option determines whether the iterative fitting is
        forced to go through the origin or not. This defaults to True, fitting var=1/gain * mean,
        if set to False then var=1/g * mean + const is fitted.

        This is really a PTC gain measurement task. See DM-14063 for results from of a comparison between
        this task's numbers and the gain values in the HSC camera model, and those measured by the PTC task
        in eotest.

        Parameters
        ----------
        dataRef : `lsst.daf.persistence.butler.Butler.dataRef`
            dataRef for the CCD for the flats to be used
        visitPairs : `list` of `tuple`
            List of visit-pairs to use, as [(v1,v2), (v3,v4)...]

        Returns
        -------
        gains : `dict` of `float`
            Dict of the as-calculated amplifier gain values, keyed by amplifier name
        nominalGains : `dict` of `float`
            Dict of the amplifier gains, as reported by the `detector` object, keyed by amplifier name
        """
        detector = dataRef.get('raw_detector')
        ampInfoCat = detector.getAmpInfoCatalog()
        ampNames = [amp.getName() for amp in ampInfoCat]

        ampMeans = {key: [] for key in ampNames}  # these get turned into np.arrays later
        ampCoVariances = {key: [] for key in ampNames}
        ampVariances = {key: [] for key in ampNames}

        # Loop over the amps in the CCD, calculating a PTC for each amplifier.
        # The amplifier iteration is performed in _calcMeansAndVars()
        # NB: no gain correction is applied
        for visPairNum, visPair in enumerate(visitPairs):
            _means, _vars, _covars = self._calcMeansAndVars(dataRef, visPair[0], visPair[1])

            # Do sanity checks; if these are failed more investigation is needed
            breaker = 0
            for amp in detector:
                ampName = amp.getName()
                if _means[ampName]*10 < _vars[ampName] or _means[ampName]*10 < _covars[ampName]:
                    self.log.warn('Sanity check failed; check visit pair %s,%s'%visPair)
                    breaker += 1
            if breaker:
                continue

            # having made sanity checks, pull the values out into the respective dicts
            for k in _means.keys():  # keys are necessarily the same
                if _vars[k]*1.3 < _covars[k] or _vars[k]*0.7 > _covars[k]:
                    self.log.warn('Dropped a value')
                    continue
                ampMeans[k].append(_means[k])
                ampVariances[k].append(_vars[k])
                ampCoVariances[k].append(_covars[k])

        gains = {}
        nomGains = {}
        for amp in detector:
            ampName = amp.getName()
            nomGains[ampName] = amp.getGain()
            slopeRaw, interceptRaw, rVal, pVal, stdErr = \
                stats.linregress(np.asarray(ampMeans[ampName]), np.asarray(ampCoVariances[ampName]))
            slopeFix, _ = self._iterativeRegression(np.asarray(ampMeans[ampName]),
                                                    np.asarray(ampCoVariances[ampName]),
                                                    fixThroughOrigin=True)
            slopeUnfix, intercept = self._iterativeRegression(np.asarray(ampMeans[ampName]),
                                                              np.asarray(ampCoVariances[ampName]),
                                                              fixThroughOrigin=False)
            self.log.info("Slope of     raw fit: %s, intercept: %s p value: %s"%(slopeRaw,
                                                                                 interceptRaw, pVal))
            self.log.info("slope of   fixed fit: %s, difference vs raw:%s"%(slopeFix,
                                                                            slopeFix-slopeRaw))
            self.log.info("slope of unfixed fit: %s, difference vs fix:%s"%(slopeUnfix,
                                                                            slopeFix-slopeUnfix))
            if self.config.fixPtcThroughOrigin:
                slopeToUse = slopeFix
            else:
                slopeToUse = slopeUnfix

            if self.debug.enabled:
                fig = plt.figure()
                ax = fig.add_subplot(111)
                ax.plot(np.asarray(ampMeans[ampName]),
                        np.asarray(ampCoVariances[ampName]), linestyle='None', marker='x', label='data')
                if self.config.fixPtcThroughOrigin:
                    ax.plot(np.asarray(ampMeans[ampName]),
                            np.asarray(ampMeans[ampName])*slopeToUse, label='Fit through origin')
                else:
                    ax.plot(np.asarray(ampMeans[ampName]),
                            np.asarray(ampMeans[ampName])*slopeToUse+intercept,
                            label='Fit (intercept unconstrained')

                dataRef.put(fig, "plotBfPtc", amp=ampName)
                self.log.info('Saved PTC to for CCD %s amp %s'%(detector.getId(), ampName))
            gains[ampName] = 1.0/slopeToUse
        return gains, nomGains

    def _calcMeansAndVars(self, dataRef, v1, v2):
        """Calculate the means, vars, covars, and retieve the nominal gains, for each amp in each ccd.

        This code runs using two visit numbers, and for ccd specified.
        It calculates the correlations in the individual amps without rescaling any gains.
        This allows a photon transfer curve to be generated and the gains measured.

        Images are assembled with use the isrTask, and basic isr is performed.

        Parameters:
        -----------
        dataRef : `lsst.daf.persistence.butler.Butler.dataRef`
            dataRef for the CCD for the repo containg the flats to be used
        v1 : `int`
            First visit of the visit pair
        v2 : `int`
            Second visit of the visit pair

        Returns
        -------
        means, vars, covars : `tuple` of `dicts`
            Three dicts, keyed by ampName, containing the sum of the image-means,
            the variance, and the quarter-image of the xcorr.
        """
        sigma = self.config.nSigmaClipGainCalc
        maxLag = self.config.maxLag
        border = self.config.nPixBorderGainCalc
        biasCorr = self.config.biasCorr

        detector = dataRef.get('raw_detector')

        ampMeans = {}

        dataRef.dataId['visit'] = v1
        exp1 = self.isr.runDataRef(dataRef).exposure
        dataRef.dataId['visit'] = v2
        exp2 = self.isr.runDataRef(dataRef).exposure
        del dataRef.dataId['visit']
        exps = [exp1, exp2]

        detector = exps[0].getDetector()  # note we lose the detector in the next line as they belong to exp
        ims = [self._convertImagelikeToFloatImage(exp) for exp in exps]

        if self.debug.display:
            self.disp1.mtv(ims[0], title=str(v1))
            self.disp2.mtv(ims[1], title=str(v2))

        sctrl = afwMath.StatisticsControl()
        sctrl.setNumSigmaClip(sigma)
        for imNum, im in enumerate(ims):

            # calculate the sigma-clipped mean, excluding the borders
            # safest to apply borders to all amps regardless of edges
            # easier, camera-agnostic, and mitigates potentially dodgy overscan-biases around edges as well
            for amp in detector:
                ampName = amp.getName()
                ampIm = im[amp.getBBox()]
                mean = afwMath.makeStatistics(ampIm[border:-border, border:-border],
                                              afwMath.MEANCLIP, sctrl).getValue()
                if ampName not in ampMeans.keys():
                    ampMeans[ampName] = []
                ampMeans[ampName].append(mean)
                ampIm -= mean

        diff = ims[0].clone()
        diff -= ims[1]

        temp = diff[border:-border, border:-border]

        # Subtract background.  It should be a constant, but it isn't always (e.g. some SuprimeCam flats)
        # TODO: Check how this looks, and if this is the "right" way to do this
        binsize = self.config.backgroundBinSize
        nx = temp.getWidth()//binsize
        ny = temp.getHeight()//binsize
        bctrl = afwMath.BackgroundControl(nx, ny, sctrl, afwMath.MEANCLIP)
        bkgd = afwMath.makeBackground(temp, bctrl)
        diff[border:-border, border:-border] -= bkgd.getImageF(afwMath.Interpolate.CUBIC_SPLINE,
                                                               afwMath.REDUCE_INTERP_ORDER)

        variances = {}
        coVars = {}
        for amp in detector:
            ampName = amp.getName()

            diffAmpIm = diff[amp.getBBox()].clone()
            diffAmpImCrop = diffAmpIm[border:-border-maxLag, border:-border-maxLag]
            diffAmpImCrop -= afwMath.makeStatistics(diffAmpImCrop, afwMath.MEANCLIP, sctrl).getValue()
            w, h = diffAmpImCrop.getDimensions()
            xcorr = np.zeros((maxLag + 1, maxLag + 1), dtype=np.float64)

            # calculate the cross correlation
            for xlag in range(maxLag + 1):
                for ylag in range(maxLag + 1):
                    dim_xy = diffAmpIm[border+xlag:border+xlag + w, border+ylag: border+ylag + h].clone()
                    dim_xy -= afwMath.makeStatistics(dim_xy, afwMath.MEANCLIP, sctrl).getValue()
                    dim_xy *= diffAmpImCrop
                    xcorr[xlag, ylag] = afwMath.makeStatistics(dim_xy,
                                                               afwMath.MEANCLIP, sctrl).getValue()/(biasCorr)

            variances[ampName] = xcorr[0, 0]
            xcorr_full = self._tileArray(xcorr)
            coVars[ampName] = np.sum(xcorr_full)

            msg = "M1: " + str(ampMeans[ampName][0])
            msg += " M2 " + str(ampMeans[ampName][1])
            msg += " M_sum: " + str((ampMeans[ampName][0])+ampMeans[ampName][1])
            msg += " Var " + str(variances[ampName])
            msg += " coVar: " + str(coVars[ampName])
            self.log.debug(msg)

            means = {}
            for amp in detector:
                ampName = amp.getName()
                means[ampName] = ampMeans[ampName][0] + ampMeans[ampName][1]

        return means, variances, coVars

    def _plotXcorr(self, xcorr, mean, zmax=0.05, title=None, fig=None, saveToFileName=None):
        """Used to plot the correlation functions."""
        try:
            xcorr = xcorr.getArray()
        except:
            pass

        xcorr /= float(mean)
        # xcorr.getArray()[0,0]=abs(xcorr.getArray()[0,0]-1)

        if fig is None:
            fig = plt.figure()
        else:
            fig.clf()

        ax = fig.add_subplot(111, projection='3d')
        ax.azim = 30
        ax.elev = 20

        nx, ny = np.shape(xcorr)

        xpos, ypos = np.meshgrid(np.arange(nx), np.arange(ny))
        xpos = xpos.flatten()
        ypos = ypos.flatten()
        zpos = np.zeros(nx*ny)
        dz = xcorr.flatten()
        dz[dz > zmax] = zmax

        ax.bar3d(xpos, ypos, zpos, 1, 1, dz, color='b', zsort='max', sort_zpos=100)
        if xcorr[0, 0] > zmax:
            ax.bar3d([0], [0], [zmax], 1, 1, 1e-4, color='c')

        ax.set_xlabel("row")
        ax.set_ylabel("column")
        ax.set_zlabel(r"$\langle{(F_i - \bar{F})(F_i - \bar{F})}\rangle/\bar{F}$")

        if title:
            fig.suptitle(title)
        if saveToFileName:
            fig.savefig(saveToFileName)

    @staticmethod
    def _getNameOfSet(vals):
        """Convert a list of numbers into a string, merging consecutive values."""
        if not vals:
            return ""

        def _addPairToName(valName, val0, val1):
            """Add a pair of values, val0 and val1, to the valName list."""
            sval1 = str(val1)
            if val0 != val1:
                pre = os.path.commonprefix([str(val0), sval1])
                sval1 = int(sval1[len(pre):])
            valName.append("%s-%s" % (val0, sval1) if val1 != val0 else str(val0))

        valName = []
        val0 = vals[0]
        val1 = val0
        for val in vals[1:]:
            if isinstance(val, int) and val == val1 + 1:
                val1 = val
            else:
                _addPairToName(valName, val0, val1)
                val0 = val
                val1 = val0

        _addPairToName(valName, val0, val1)

        return ", ".join(valName)

    def _iterativeRegression(self, x, y, fixThroughOrigin=False, nSigmaClip=None, maxIter=None):
        """Use linear regression to fit a line of best fit, iteratively removing outliers.

        Useful when you have a sufficiently large numbers of points on your PTC.
        Function iterates until either there are no outliers of nSigmaClip magnitude, or until the specified
        maximum number of iterations has been performed.

        Parameters:
        -----------
        x : `numpy.array`
            The independent variable. Must be a numpy array, not a list.
        y : `numpy.array`
            The dependent variable. Must be a numpy array, not a list.
        fixThroughOrigin : `bool`, optional
            Whether to fix the PTC through the origin or allow an y-intercept.
        nSigmaClip : `float`, optional
            The number of sigma to clip to. Pulled from the task config if not specified.
        maxIter : `int`, optional
            The maximum number of iterations allowed. Pulled from the task config if not specified.

        Returns:
        --------
        slope : `float`
            The slope of the line of best fit
        intercept : `float`
            The y-intercept of the line of best fit
        """
        if not maxIter:
            maxIter = self.config.maxIterRegression
        if not nSigmaClip:
            nSigmaClip = self.config.nSigmaClipRegression

        nIter = 0
        sctrl = afwMath.StatisticsControl()
        sctrl.setNumSigmaClip(nSigmaClip)

        if fixThroughOrigin:
            while nIter < maxIter:
                nIter += 1
                self.log.debug("Origin fixed, iteration # %s using %s elements:"%(nIter, np.shape(x)[0]))
                TEST = x[:, np.newaxis]
                slope, _, _, _ = np.linalg.lstsq(TEST, y)
                slope = slope[0]
                res = y - slope * x
                resMean = afwMath.makeStatistics(res, afwMath.MEANCLIP, sctrl).getValue()
                resStd = np.sqrt(afwMath.makeStatistics(res, afwMath.VARIANCECLIP, sctrl).getValue())
                # xxx check this line performs the same
                index = np.where((res > (resMean+nSigmaClip*resStd)) | (res < (resMean-nSigmaClip*resStd)))
                self.log.debug("%.3f %.3f %.3f %.3f"%(resMean, resStd, np.max(res), nSigmaClip))
                if np.shape(np.where(index))[1] == 0 or (nIter >= maxIter):  # run out of points or iters
                    break
                x = np.delete(x, index)
                y = np.delete(y, index)

            return slope, 0

        while nIter < maxIter:
            nIter += 1
            self.log.debug("Iteration # %s using %s elements:"%(nIter, np.shape(x)[0]))
            xx = np.vstack([x, np.ones(len(x))]).T
            ret, _, _, _ = np.linalg.lstsq(xx, y)
            slope, intercept = ret
            res = y - slope*x - intercept
            resMean = afwMath.makeStatistics(res, afwMath.MEANCLIP, sctrl).getValue()
            resStd = np.sqrt(afwMath.makeStatistics(res, afwMath.VARIANCECLIP, sctrl).getValue())
            index = np.where((res > (resMean + nSigmaClip * resStd)) | (res < resMean - nSigmaClip * resStd))
            self.log.debug("%.3f %.3f %.3f %.3f"%(resMean, resStd, np.max(res), nSigmaClip))
            if np.shape(np.where(index))[1] == 0 or (nIter >= maxIter):  # run out of points, or iterations
                break
            x = np.delete(x, index)
            y = np.delete(y, index)

        return slope, intercept

    def _generateKernel(self, corrs, means, objId, rejectLevel=None):
        """Generate the full kernel from a list of (gain-corrected) cross-correlations and means.

        Taking a list of quarter-image, gain-corrected cross-correlations, do a pixel-wise sigma-clipped
        mean of each, and tile into the full-sized kernel image.

        Each corr in corrs is one quarter of the full cross-correlation, and has been gain-corrected.
        Each mean in means is a tuple of the means of the two individual images, corresponding to that corr.

        Parameters:
        -----------
        corrs : `list` of `numpy.ndarray`, (Ny, Nx)
            A list of the quarter-image cross-correlations
        means : `dict` of `tuples` of `floats`
            The means of the input images for each corr in corrs
        rejectLevel : `float`, optional
            This is essentially is a sanity check parameter.
            If this condition is violated there is something unexpected going on in the image, and it is
            discarded from the stack before the clipped-mean is calculated.

        Returns:
        --------
        kernel : `numpy.ndarray`, (Ny, Nx)
            The output kernel
        """
        if not rejectLevel:
            rejectLevel = self.config.xcorrCheckRejectLevel

        # Try to average over a set of possible inputs. This generates a simple function of the kernel that
        # should be constant across the images, and averages that.
        xcorrList = []
        sctrl = afwMath.StatisticsControl()
        sctrl.setNumSigmaClip(self.config.nSigmaClipKernelGen)

        for corrNum, ((mean1, mean2), corr) in enumerate(zip(means, corrs)):
            corr[0, 0] -= (mean1+mean2)
            if corr[0, 0] > 0:
                self.log.warn('Skipped item %s due to unexpected value of (variance-mean)'%corrNum)
                continue
            corr /= -float(1.0*(mean1**2+mean2**2))

            fullCorr = self._tileArray(corr)

            xcorrCheck = np.abs(np.sum(fullCorr))/np.sum(np.abs(fullCorr))
            if xcorrCheck > rejectLevel:
                self.log.warn("Sum of the xcorr is unexpectedly high. Investigate item num %s for %s. \n"
                              "value = %s"%(corrNum, objId, xcorrCheck))
                continue
            xcorrList.append(fullCorr)

        if not xcorrList:
            raise RuntimeError("Cannot generate kernel because all inputs were discarded. "
                               "Either the data is bad, or config.xcorrCheckRejectLevel is too low")

        # stack the individual xcorrs and apply a per-pixel clipped-mean
        meanXcorr = np.zeros_like(fullCorr)
        xcorrList = np.transpose(xcorrList)
        for i in range(np.shape(meanXcorr)[0]):
            for j in range(np.shape(meanXcorr)[1]):
                meanXcorr[i, j] = afwMath.makeStatistics(xcorrList[i, j], afwMath.MEANCLIP, sctrl).getValue()

        return self._SOR(meanXcorr)

    def _SOR(self, source, maxIter=None, eLevel=None):
        """An implementation of the successive over relaxation (SOR) method.

        Parameters:
        -----------
        source : `numpy.ndarray`
            The input array
        maxIter : `int`, optional
            Maximum number of iterations to attempt before aborting
        eLevel : `float`, optional
            The target error level factor at which we deem convergence to have occured

        Returns:
        --------
        output : `numpy.ndarray`
            The solution
        """
        if not maxIter:
            maxIter = self.config.maxIterSOR
        if not eLevel:
            eLevel = self.config.eLevelSOR

        # initialise, and set boundary conditions
        func = np.zeros([source.shape[0]+2, source.shape[1]+2])
        resid = np.zeros([source.shape[0]+2, source.shape[1]+2])
        rhoSpe = np.cos(np.pi/source.shape[0])  # Here a square grid is assummed

        inError = 0
        # Calculate the initial error
        for i in range(1, func.shape[0]-1):
            for j in range(1, func.shape[1]-1):
                resid[i, j] = (func[i, j-1]+func[i, j+1]+func[i-1, j] +
                               func[i+1, j]-4*func[i, j]-source[i-1, j-1])
        inError = np.sum(np.abs(resid))

        # Iterate until convergence
        # We perform two sweeps per cycle, updating 'odd' and 'even' points separately
        nIter = 0
        omega = 1.0
        dx = 1.0
        while nIter < maxIter*2:
            outError = 0
            if nIter%2 == 0:
                for i in range(1, func.shape[0]-1, 2):
                    for j in range(1, func.shape[0]-1, 2):
                        resid[i, j] = float(func[i, j-1]+func[i, j+1]+func[i-1, j] +
                                            func[i+1, j]-4.0*func[i, j]-dx*dx*source[i-1, j-1])
                        func[i, j] += omega*resid[i, j]*.25
                for i in range(2, func.shape[0]-1, 2):
                    for j in range(2, func.shape[0]-1, 2):
                        resid[i, j] = float(func[i, j-1]+func[i, j+1]+func[i-1, j] +
                                            func[i+1, j]-4.0*func[i, j]-dx*dx*source[i-1, j-1])
                        func[i, j] += omega*resid[i, j]*.25
            else:
                for i in range(1, func.shape[0]-1, 2):
                    for j in range(2, func.shape[0]-1, 2):
                        resid[i, j] = float(func[i, j-1]+func[i, j+1]+func[i-1, j] +
                                            func[i+1, j]-4.0*func[i, j]-dx*dx*source[i-1, j-1])
                        func[i, j] += omega*resid[i, j]*.25
                for i in range(2, func.shape[0]-1, 2):
                    for j in range(1, func.shape[0]-1, 2):
                        resid[i, j] = float(func[i, j-1]+func[i, j+1]+func[i-1, j] +
                                            func[i+1, j]-4.0*func[i, j]-dx*dx*source[i-1, j-1])
                        func[i, j] += omega*resid[i, j]*.25
            outError = np.sum(np.abs(resid))
            if outError < inError*eLevel:
                break
            if nIter == 0:
                omega = 1.0/(1-rhoSpe*rhoSpe/2.0)
            else:
                omega = 1.0/(1-rhoSpe*rhoSpe*omega/4.0)
            nIter += 1

        if nIter >= maxIter*2:
            self.log.warn("Failure: SOR did not converge in %s iterations.\noutError: %s, inError: "
                          "%s,"%(nIter//2, outError, inError*eLevel))
        else:
            self.log.info("Success: SOR converged in %s iterations.\noutError: %s, inError: "
                          "%s", nIter//2, outError, inError*eLevel)
        return func[1:-1, 1:-1]

    @staticmethod
    def _tileArray(in_array):
        """Given a square input quarter-image, tile/mirror it, returning the full image.

        Given an input of side-length n, of the form

        input = array([[1, 2, 3],
                       [4, 5, 6],
                       [7, 8, 9]])

        return an array of size 2n-1 as

        output = array([[ 9,  8,  7,  8,  9],
                        [ 6,  5,  4,  5,  6],
                        [ 3,  2,  1,  2,  3],
                        [ 6,  5,  4,  5,  6],
                        [ 9,  8,  7,  8,  9]])

        Parameters:
        -----------
        input : `np.array`
            The square input quarter-array

        Returns:
        --------
        output : `np.array`
            The full, tiled array
        """
        assert(in_array.shape[0] == in_array.shape[1])
        length = in_array.shape[0]-1
        output = np.zeros((2*length+1, 2*length+1))

        for i in range(length+1):
            for j in range(length+1):
                output[i+length, j+length] = in_array[i, j]
                output[-i+length, j+length] = in_array[i, j]
                output[i+length, -j+length] = in_array[i, j]
                output[-i+length, -j+length] = in_array[i, j]
        return output

    @staticmethod
    def _convertImagelikeToFloatImage(imagelikeObject):
        """Turn an exposure or masked image of any type into an ImageF."""
        for attr in ("getMaskedImage", "getImage"):
            if hasattr(imagelikeObject, attr):
                imagelikeObject = getattr(imagelikeObject, attr)()
        try:
            floatImage = imagelikeObject.convertF()
        except AttributeError:
            raise RuntimeError("Failed to convert image to float")
        return floatImage


def _crossCorrelateSimulate(im, im2, n=5, border=10, sigma=5):
    """Perform a simple xcorr from two images.

    This sim code is used to estimate the bias correction used above.

    xxx need to work out why this performs differently,
    which version is correct, and whether this function needs to exist at all.
    I don't think it does, as the real code is now modular and doesn't have to run ISR.

    It contains many elements of the actual code
    above (without individual amps and ISR removal )
    It takes two images, im and im2;
    n the max lag of the correlation function; border, the number of border
    pixels to discard; and sigma the sigma to use in the mean clip.
    """
    sctrl = afwMath.StatisticsControl()
    sctrl.setNumSigmaClip(sigma)
    # im = self._convertImagelikeToFloatImage(im)
    # im2 = self._convertImagelikeToFloatImage(im2)

    means1 = [0, 0]
    means1[0] = afwMath.makeStatistics(im[border:-border, border:-border],
                                       afwMath.MEANCLIP, sctrl).getValue()
    means1[1] = afwMath.makeStatistics(im2[border:-border, border:-border],
                                       afwMath.MEANCLIP, sctrl).getValue()
    im -= means1[0]
    im2 -= means1[1]
    diff = im2.clone()
    diff -= im.clone()
    diff = diff[border:-border, border:-border]
    binsize = 128  # this needs to change somehow. Where should this be got from?
    nx = diff.getWidth()//binsize
    ny = diff.getHeight()//binsize
    bctrl = afwMath.BackgroundControl(nx, ny, sctrl, afwMath.MEANCLIP)
    bkgd = afwMath.makeBackground(diff, bctrl)
    diff -= bkgd.getImageF(afwMath.Interpolate.CUBIC_SPLINE, afwMath.REDUCE_INTERP_ORDER)
    dim0 = diff[0: -n, : -n].clone()
    dim0 -= afwMath.makeStatistics(dim0, afwMath.MEANCLIP, sctrl).getValue()
    w, h = dim0.getDimensions()
    xcorr = afwImage.ImageD(n + 1, n + 1)
    for di in range(n + 1):
        for dj in range(n + 1):
            dim_ij = diff[di:di + w, dj: dj + h].clone()
            dim_ij -= afwMath.makeStatistics(dim_ij, afwMath.MEANCLIP, sctrl).getValue()

            dim_ij *= dim0
            xcorr[di, dj] = afwMath.makeStatistics(dim_ij, afwMath.MEANCLIP, sctrl).getValue()
    L = np.shape(xcorr.getArray())[0]-1
    XCORR = np.zeros([2*L+1, 2*L+1])
    for i in range(L+1):
        for j in range(L+1):
            XCORR[i+L, j+L] = xcorr.getArray()[i, j]
            XCORR[-i+L, j+L] = xcorr.getArray()[i, j]
            XCORR[i+L, -j+L] = xcorr.getArray()[i, j]
            XCORR[-i+L, -j+L] = xcorr.getArray()[i, j]
    return xcorr, np.sum(means1)


def simulateXcorr(fluxLevels, imageShape, addCorrelations=False, correlationStrength=0.1, nSigma=5, border=3,
                  repeats=2, seed=0):
    """Fill images of specified size with Poisson-distributed values with means fluxLevels.

    Parameters:
    -----------
    fluxLevels : `list` of `int`
        The mean flux levels at which to simiulate. Nominal values might be something like
        [70000, 90000, 110000]
    imageShape : `tuple` of `int`
        The shape of the image array to simulate, nx by ny pixels.
    addCorrelations : `bool`, optional
        Whether to add brighter-fatter like correlations to the simulated images.
        If true, a correlation between x_{i,j} and x_{i+1,j+1} is introduced
        by adding a*x_{i,j} to x_{i+1,j+1}
    correlationStrength : `float`, optional
        The strength of the correlations. This is the value of the coefficient `a` in the above definition.
    nSigma : `float`, optional
        Number of sigma to clip to when calculating the sigma-clipped mean.
    border : `int`, optional
        Number of border pixels to mask
    repeats : `int`, optional
        Number of repeats to perform so that results can be averaged to improve SNR.
    seed : `int`, optional
        The random seed to use for the Poisson points.

    Returns:
    --------
    means : ``
        xxx
    xcorrs : ``
        xxx
    """
    means = {f: [] for f in fluxLevels}
    xcorrs = {f: [] for f in fluxLevels}

    random = np.random.RandomState(seed)

    for rep in range(repeats):
        for flux in fluxLevels:
            im0 = afwImage.ImageF(imageShape[1], imageShape[0])  # backwards here because of numpy call next
            im1 = afwImage.ImageF(imageShape[1], imageShape[0])  # needs to match for broadcast
            im0.getArray()[:, :] = random.poisson(flux, (imageShape))
            im1.getArray()[:, :] = random.poisson(flux, (imageShape))
            if addCorrelations:
                im0[1:, 1:] += correlationStrength*im0[:-1, :-1]
                im1[1:, 1:] += correlationStrength*im1[:-1, :-1]

            _xcorr, _means = _crossCorrelateSimulate(im0, im1, border=border, sigma=nSigma)
            means[flux].append(_means)
            xcorrs[flux].append(_xcorr)
            # if addCorrelations:
            #     self.log.debug("Simulated/Expected:", flux, means[flux][-1], '\n',
            #                    (xcorrs[flux][-1][1, 1]/means[flux][-1]*(1+correlationStrength))/.1)
            #     # xxx the 0.1 in the above should be correlationStrength, right?
            # else:
            #     self.log.debug("Simulated/Expected:", flux, means[flux][-1], '\n',
            #                    xcorrs[flux][-1][0, 0]/means[flux][-1])
    return means, xcorrs
