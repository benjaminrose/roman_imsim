"""
Interface to obtain objects from skyCatalogs.
"""
import os
import numpy as np
import galsim
import galsim.roman as roman
from galsim.config import InputLoader, RegisterInputType, RegisterValueType, \
    RegisterObjectType
from skycatalogs import skyCatalogs


class SkyCatalogInterface:
    """Interface to skyCatalogs package."""

    def __init__(self, file_name, exptime, wcs=None, mjd=None, collecting_area=1., bandpass = None, xsize=None, ysize=None,
                 obj_types=None, edge_pix=100, max_flux=None, logger=None):
        """
        Parameters
        ----------
        file_name : str
            Name of skyCatalogs yaml config file.
        wcs : galsim.WCS
            WCS of the image to render.
        mjd : float
            MJD of the midpoint of the exposure.
        exptime : float
            Exposure time.
        collecting_area : float
            Collecting area of the telescope.
        xsize : int
            Size in pixels of CCD in x-direction.
        ysize : int
            Size in pixels of CCD in y-direction.
        obj_types : list-like [None]
            List or tuple of object types to render, e.g., ('star', 'galaxy').
            If None, then consider all object types.
        edge_pix : float [100]
            Size in pixels of the buffer region around nominal image
            to consider objects.
        logger : logging.Logger [None]
            Logger object.
        """
        self.file_name = file_name
        self.wcs = wcs
        self.mjd = mjd
        self.exptime = exptime
        self.collecting_area = collecting_area
        self.bandpass = bandpass
        if xsize is not None:
            self.xsize = xsize
        else:
            self.xsize = roman.n_pix
        if ysize is not None:
            self.ysize = ysize
        else:
            self.ysize = roman.n_pix
        self.obj_types = obj_types
        self.edge_pix = edge_pix
        self.logger = galsim.config.LoggerWrapper(logger)

        if obj_types is not None:
            self.logger.warning(f'Object types restricted to {obj_types}')
        self.sca_center = wcs.toWorld(galsim.PositionD(self.xsize/2.0, self.ysize/2.0))
        self._objects = None

    @property
    def objects(self):
        if self._objects is None:
            # Select objects from polygonal region bounded by CCD edges
            corners = ((-self.edge_pix, -self.edge_pix),
                       (self.xsize + self.edge_pix, -self.edge_pix),
                       (self.xsize + self.edge_pix, self.ysize + self.edge_pix),
                       (-self.edge_pix, self.ysize + self.edge_pix))
            vertices = []
            for x, y in corners:
                sky_coord = self.wcs.toWorld(galsim.PositionD(x, y))
                vertices.append((sky_coord.ra/galsim.degrees,
                                 sky_coord.dec/galsim.degrees))
            region = skyCatalogs.PolygonalRegion(vertices)
            sky_cat = skyCatalogs.open_catalog(
                self.file_name)
            self._objects = sky_cat.get_objects_by_region(
                region, obj_type_set=self.obj_types, mjd=self.mjd)
            if not self._objects:
                self.logger.warning("No objects found on image.")
        return self._objects

    def get_sca_center(self):
        """
        Return the SCA center.
        """
        return self.sca_center

    def getNObjects(self):
        """
        Return the number of GSObjects to render
        """
        return len(self.objects)

    def getApproxNObjects(self):
        """
        Return the approximate number of GSObjects to render, as set in
        the class initializer.
        """
        return self.getNObjects()

    def getWorldPos(self, index):
        """
        Return the sky coordinates of the skyCatalog object
        corresponding to the specified index.

        Parameters
        ----------
        index : int
            Index of the (object_index, subcomponent) combination.

        Returns
        -------
        galsim.CelestialCoord
        """
        skycat_obj = self.objects[index]
        ra, dec = skycat_obj.ra, skycat_obj.dec
        return galsim.CelestialCoord(ra*galsim.degrees, dec*galsim.degrees)

    def getObj(self, index, gsparams=None, rng=None, exptime=30):
        """
        Return the galsim object for the skyCatalog object
        corresponding to the specified index.  If the skyCatalog
        object is a galaxy, the returned galsim object will be
        a galsim.Sum.

        Parameters
        ----------
        index : int
            Index of the object in the self.objects catalog.

        Returns
        -------
        galsim.GSObject
        """
        if not self.objects:
            raise RuntimeError("Trying to get an object from an empty sky catalog")

        skycat_obj = self.objects[index]
        gsobjs = skycat_obj.get_gsobject_components(gsparams, rng)

        if True and skycat_obj.object_type == 'galaxy':
            # Apply DC2 dilation to the individual galaxy components.
            for component, gsobj in gsobjs.items():
                comp = component if component != 'knots' else 'disk'
                a = skycat_obj.get_native_attribute(f'size_{comp}_true')
                b = skycat_obj.get_native_attribute(f'size_minor_{comp}_true')
                scale = np.sqrt(a/b)
                gsobjs[component] = gsobj.dilate(scale)
                logger.info('doing dilation %d',scale)

        seds = skycat_obj.get_observer_sed_components(mjd=self.mjd)

        gs_obj_list = []
        for component in gsobjs:
            if component in seds:
                gs_obj_list.append(gsobjs[component]*seds[component]
                                   *self.exptime*self.collecting_area)

        if not gs_obj_list:
            return None

        if len(gs_obj_list) == 1:
            gs_object = gs_obj_list[0]
        else:
            gs_object = galsim.Add(gs_obj_list)

        # Compute the flux or get the cached value.
        gs_object.flux \
            = gs_object.calculateFlux(self.bandpass)

        return gs_object


class SkyCatalogLoader(InputLoader):
    """
    Class to load SkyCatalogInterface object.
    """
    def getKwargs(self, config, base, logger):
        req = {'file_name': str, 'exptime' : float}
        opt = {
               'edge_pix' : float,
               'obj_types' : list,
               'mjd': float,
               'collecting_area' : float,
              }
        kwargs, safe = galsim.config.GetAllParams(config, base, req=req,
                                                  opt=opt)
        wcs = galsim.config.BuildWCS(base['image'], 'wcs', base, logger=logger)
        kwargs['wcs'] = wcs
        kwargs['logger'] = logger

        if 'bandpass' not in config:
            base['bandpass'] = galsim.config.BuildBandpass(base['image'], 'bandpass', base, logger=logger)[0]

        kwargs['bandpass'] = base['bandpass']
        # Sky catalog object lists are created per CCD, so they are
        # not safe to reuse.
        safe = False
        return kwargs, safe


def SkyCatObj(config, base, ignore, gsparams, logger):
    """
    Build an object according to info in the sky catalog.
    """
    skycat = galsim.config.GetInputObj('sky_catalog', config, base, 'SkyCatObj')

    # Ensure that this sky catalog matches the CCD being simulated by
    # comparing center locations on the sky.
    world_center = base['world_center']
    sca_center = skycat.get_sca_center()
    sep = sca_center.distanceTo(base['world_center'])/galsim.arcsec
    # Centers must agree to within at least 1 arcsec:
    if sep > 1.0:
        message = ("skyCatalogs selection and SCA center do not agree: \n"
                   "skycat.sca_center: "
                   f"{sca_center.ra/galsim.degrees:.5f}, "
                   f"{sca_center.dec/galsim.degrees:.5f}\n"
                   "world_center: "
                   f"{world_center.ra/galsim.degrees:.5f}, "
                   f"{world_center.dec/galsim.degrees:.5f} \n"
                   f"Separation: {sep:.2e} arcsec")
        raise RuntimeError(message)

    # Setup the indexing sequence if it hasn't been specified.  The
    # normal thing with a catalog is to just use each object in order,
    # so we don't require the user to specify that by hand.  We can do
    # it for them.
    galsim.config.SetDefaultIndex(config, skycat.getNObjects())

    req = { 'index' : int }
    opt = { 'num' : int }
    kwargs, safe = galsim.config.GetAllParams(config, base, req=req, opt=opt)
    index = kwargs['index']

    rng = galsim.config.GetRNG(config, base, logger, 'SkyCatObj')

    obj = skycat.getObj(index, gsparams=gsparams, rng=rng)
    base['object_id'] = skycat.objects[index].id

    return obj, safe


def SkyCatWorldPos(config, base, value_type):
    """Return a value from the object part of the skyCatalog
    """
    skycat = galsim.config.GetInputObj('sky_catalog', config, base,
                                       'SkyCatWorldPos')

    # Setup the indexing sequence if it hasn't been specified.  The
    # normal thing with a catalog is to just use each object in order,
    # so we don't require the user to specify that by hand.  We can do
    # it for them.
    galsim.config.SetDefaultIndex(config, skycat.getNObjects())

    req = { 'index' : int }
    opt = { 'num' : int }
    kwargs, safe = galsim.config.GetAllParams(config, base, req=req, opt=opt)
    index = kwargs['index']

    pos = skycat.getWorldPos(index)
    return pos, safe


RegisterInputType('sky_catalog',
                  SkyCatalogLoader(SkyCatalogInterface, has_nobj=True))
RegisterObjectType('SkyCatObj', SkyCatObj, input_type='sky_catalog')
RegisterValueType('SkyCatWorldPos', SkyCatWorldPos, [galsim.CelestialCoord],
                  input_type='sky_catalog')