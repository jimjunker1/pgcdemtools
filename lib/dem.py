#!/usr/bin/env python

"""
dem raster information class and methods
"""

import os, sys, string, shutil, glob, re, logging,math
from datetime import *
import gdal, osr, ogr, gdalconst
from collections import namedtuple
import numpy
from numpy import flatnonzero
import utils

gdal.UseExceptions()

#### Create Logger
logger = logging.getLogger("logger")
logger.setLevel(logging.DEBUG)

__all__ = [
    "SetsmDem",
    "AspDem",
    "SetsmTile",
    "RegInfo"
]

epsgs = [
    3031,
    3413,
]

srs_wgs84 = utils.osr_srs_preserve_axis_order(osr.SpatialReference())
srs_wgs84.ImportFromEPSG(4326)

### build wgs84 utm epsgs
for x in range(6,8):
    for y in range(1,60):
        epsg = 32000 + x*100 + y
        epsgs.append(epsg)

#### Strip DEM name pattern
setsm_scene_pattern = re.compile("""(?P<pairname>
                                    (?P<sensor>[A-Z][A-Z\d]{2}\d)_
                                    (?P<timestamp>\d{8})_
                                    (?P<catid1>[A-Z0-9]{16})_
                                    (?P<catid2>[A-Z0-9]{16})
                                    )_
                                    (?P<tile1>R\d+C\d+)?-?
                                    (?P<order1>\d{12}_\d{2}_P\d{3})_
                                    (?P<tile2>R\d+C\d+)?-?
                                    (?P<order2>\d{12}_\d{2}_P\d{3})_
                                    (?P<res>[0128])_
                                    meta.txt\Z""", re.I | re.X)

setsm_strip_pattern = re.compile("""(?P<pairname>
                                    (?P<sensor>[A-Z][A-Z\d]{2}\d)_
                                    (?P<timestamp>\d{8})_
                                    (?P<catid1>[A-Z0-9]{16})_
                                    (?P<catid2>[A-Z0-9]{16})
                                    )_
                                    (?P<res>(\d+|0\.\d+)c?m)_
                                    (lsf_)?
                                    (?P<partnum>[SEG\d]+)_
                                    ((?P<version>v[\d/.]+)_)?
                                    dem.(tif|jpg)\Z""", re.I | re.X)

setsm_strip_pattern2 = re.compile("""(?P<pairname>
                                    (?P<sensor>[A-Z][A-Z\d]{2}\d)_
                                    (?P<timestamp>\d{8})_
                                    (?P<catid1>[A-Z0-9]{16})_
                                    (?P<catid2>[A-Z0-9]{16})
                                    )_
                                    (?P<partnum>[SEG\d]+)_
                                    (?P<res>(\d+|0\.\d+)c?m)_
                                    ((?P<version>v[\d/.]+)_)?
                                    (lsf_)?
                                    dem.(tif|jpg)\Z""", re.I | re.X)

asp_strip_pattern = re.compile("""(?P<pairname>
                                  (?P<sensor>[A-Z]{2}\d{2})_
                                  (?P<timestamp>\d{8})_
                                  (?P<catid1>[A-Z0-9]{16})_
                                  (?P<catid2>[A-Z0-9]{16}))_?
                                  (?P<res>\d+m)?-DEM.(tif|jpg)\Z""", re.I | re.X)

setsm_tile_pattern = re.compile("""(?P<tile>\d+_\d+)_
                                   ((?P<subtile>\d+_\d+)_)?
                                   (?P<res>(\d+|0\.\d+)c?m)_
                                   ((?P<version>v[\d/.]+)_)?
                                   (reg_)?
                                   dem.tif\Z""", re.I| re.X)


class SetsmScene(object):
    def __init__(self,metapath,md=None):

        ## If md dictionary is passed in, recreate object from dict instead of from file location
        if md:
            self._rebuild_scene_from_dict(md)

        else:
            self.srcdir, self.srcfn = os.path.split(metapath)
            self.sceneid = self.srcfn[:-9]

            self.metapath = metapath
            self.lsf_dem = os.path.join(self.srcdir,self.sceneid+"_dem_smooth.tif")
            self.dem = os.path.join(self.srcdir,self.sceneid+"_dem.tif")
            self.matchtag = os.path.join(self.srcdir,self.sceneid+"_matchtag.tif")
            self.ortho = os.path.join(self.srcdir,self.sceneid+"_ortho.tif")

            # set shared attributes
            self.id = self.sceneid
            self.srcfp = self.metapath

            if not os.path.isfile(self.ortho) \
            or not os.path.isfile(self.matchtag) \
            or not os.path.isfile(self.metapath) \
            or not (os.path.isfile(self.dem) or os.path.isfile(self.lsf_dem)):
                raise RuntimeError("DEM is part of an incomplete set: {}".format(self.sceneid))

            #### parse name
            match = setsm_scene_pattern.match(self.srcfn)
            if match:
                groups = match.groupdict()
                self.pairname = groups['pairname']
                self.catid1 = groups['catid1']
                self.catid2 = groups['catid2']
                self.acqdate1 = datetime.strptime(groups['timestamp'], '%Y%m%d') # if present, the metadata file value will overwrite this
                self.acqdate2 = self.acqdate1
                self.sensor1 = groups['sensor'] # if present, the metadata file value will overwrite this
                self.sensor2 = self.sensor1
                self.res = groups['res']
                self.creation_date = None
                self.algm_version = 'SETSM' # if present, the metadata file value will overwrite this
                self.geom = None
                self.group_version = None
            else:
                raise RuntimeError("DEM name does not match expected pattern: {}".format(self.srcfn))

            ## Read metadata file
            self.get_metafile_info()

            ## Build res_str
            try:
                res_int = int(self.res)
            except ValueError as e:
                res_str = self.res
            else:
                if res_int > 0:
                    self.res_str = "{}m".format(self.res)
                elif res_int == 0:
                    self.res_str = "50cm"
                else:
                    raise RuntimeError("Scene has invalid resolution value in name: {}".format(scene.sceneid))

            ## Get version str with ability to handle 1-3 parts of semantic version
            if self.group_version:
                vp = self.group_version.split('.')
            else:
                vp = self.version.split('.')

            vl = [0,0,0]
            for i in range(len(vp)):
                vl[i] = int(vp[i])
            version_str = '{:02}{:02}{:02}'.format(vl[0],vl[1],vl[2])

            ## Make strip ID
            self.stripid = '{}_{}_v{}'.format(self.pairname,self.res_str,version_str)

    def get_dem_info(self):

        try:
            self.filesz_dem = os.path.getsize(self.dem) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_dem = 0

        try:
            self.filesz_lsf = os.path.getsize(self.lsf_dem) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_lsf = 0

        try:
            self.filesz_mt = os.path.getsize(self.matchtag) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_mt = 0

        try:
            self.filesz_or = os.path.getsize(self.ortho) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_or = 0

        if os.path.isfile(self.lsf_dem):
            dsp = self.lsf_dem
        elif os.path.isfile(self.dem):
            dsp = self.dem
        else:
            raise RuntimeError("DEM file does not exist for scene {}".format(self.sceneid))

        ds = gdal.Open(dsp)
        if ds is not None:
            self.xsize = ds.RasterXSize
            self.ysize = ds.RasterYSize
            self.proj = ds.GetProjectionRef() if ds.GetProjectionRef() != '' else ds.GetGCPProjection()
            self.gtf = ds.GetGeoTransform()

            #print raster.proj
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(self.proj)
            self.srs = src_srs
            self.proj4 = src_srs.ExportToProj4()
            #print self.proj4
            self.epsg = ''

            for epsg in epsgs:
                tgt_srs = osr.SpatialReference()
                tgt_srs.ImportFromEPSG(epsg)
                #print epsg
                #print src_srs.IsSame(tgt_srs)
                if src_srs.IsSame(tgt_srs) == 1:
                    self.epsg = epsg

            src_srs.MorphToESRI()
            self.wkt_esri = src_srs.ExportToWkt()

            self.bands = ds.RasterCount
            self.datatype = ds.GetRasterBand(1).DataType
            self.datatype_readable = gdal.GetDataTypeName(self.datatype)
            self.ndv = ds.GetRasterBand(1).GetNoDataValue()

            num_gcps = ds.GetGCPCount()

            if num_gcps == 0:

                self.xres = abs(self.gtf[1])
                self.yres = abs(self.gtf[5])
                ulx = self.gtf[0] + 0 * self.gtf[1] + 0 * self.gtf[2]
                uly = self.gtf[3] + 0 * self.gtf[4] + 0 * self.gtf[5]
                urx = self.gtf[0] + self.xsize * self.gtf[1] + 0 * self.gtf[2]
                ury = self.gtf[3] + self.xsize * self.gtf[4] + 0 * self.gtf[5]
                llx = self.gtf[0] + 0 * self.gtf[1] + self.ysize * self.gtf[2]
                lly = self.gtf[3] + 0 * self.gtf[4] + self.ysize * self.gtf[5]
                lrx = self.gtf[0] + self.xsize * self.gtf[1] + self.ysize* self.gtf[2]
                lry = self.gtf[3] + self.xsize * self.gtf[4] + self.ysize * self.gtf[5]

            elif num_gcps == 4:

                gcps = ds.GetGCPs()
                gcp_dict = {}
                id_dict = {"UpperLeft":1,
                           "1":1,
                           "UpperRight":2,
                           "2":2,
                           "LowerLeft":4,
                           "4":4,
                           "LowerRight":3,
                           "3":3}

                for gcp in gcps:
                    gcp_dict[id_dict[gcp.Id]] = [float(gcp.GCPPixel), float(gcp.GCPLine), float(gcp.GCPX), float(gcp.GCPY), float(gcp.GCPZ)]

                ulx = gcp_dict[1][2]
                uly = gcp_dict[1][3]
                urx = gcp_dict[2][2]
                ury = gcp_dict[2][3]
                llx = gcp_dict[4][2]
                lly = gcp_dict[4][3]
                lrx = gcp_dict[3][2]
                lry = gcp_dict[3][3]

                self.xres = abs(math.sqrt((ulx - urx)**2 + (uly - ury)**2)/ self.xsize)
                self.yres = abs(math.sqrt((ulx - llx)**2 + (uly - lly)**2)/ self.ysize)

            poly_wkt = 'POLYGON (( %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f ))' %(ulx,uly,urx,ury,lrx,lry,llx,lly,ulx,uly)
            self.geom = ogr.CreateGeometryFromWkt(poly_wkt)

        else:
            raise RuntimeError("Cannot open image: %s" %dsp)

        ds = None

    def get_metafile_info(self):

        ## If metafile exists
        if self.metapath:
            metad = self._parse_metadata_file()

            if 'output_projection' in metad:
                self.proj4_meta = metad['output_projection'].replace("'","")
            else:
                raise RuntimeError('Key "Output Projection" not found in meta dict from {}'.format(self.metapath))

            if 'creation_date' in metad:
                self.creation_date = self._parse_creation_date(metad['creation_date'])
            else:
                raise RuntimeError('Key "Creation Date" not found in meta dict from {}'.format(self.metapath))

            if 'setsm_version' in metad:
                self.algm_version = "SETSM {}".format(metad['setsm_version'])
                self.version = metad['setsm_version']
            else:
                raise RuntimeError('Key "SETSM Version" not found in meta dict from {}'.format(self.metapath))

            if 'group_version' in metad:
                self.group_version = metad['group_version']

            if 'image_1_acquisition_time' in metad:
                self.acqdate1 = datetime.strptime(metad["image_1_acquisition_time"], "%Y-%m-%dT%H:%M:%S.%fZ")

            if 'image_2_acquisition_time' in metad:
                self.acqdate2 = datetime.strptime(metad["image_2_acquisition_time"], "%Y-%m-%dT%H:%M:%S.%fZ")

            if 'image_1_satid' in metad:
                self.sensor1 = metad['image_1_satid']

            if 'image_2_satid' in metad:
                self.sensor2 = metad['image_2_satid']

        else:
            raise RuntimeError("meta.txt file does not exist for DEM")

    def _parse_metadata_file(self):
        metad = {}

        mdf = open(self.metapath,'r')
        for line in mdf.readlines():
            l = line.strip()
            if '=' in l:
                if l.startswith('Output Projection'):
                    key = 'output_projection'
                    val = l[l.find('=')+1:]
                    metad[key.strip()] = val.strip()
                else:
                    try:
                        key,val = l.split('=')
                    except ValueError, e:
                        logger.error('Cannot split line on "=" - {}, {}, {}'.format(l,e,self.metapath))
                    else:
                        key = key.strip().replace(" ","_").lower()
                        metad[key] = val.strip()

        mdf.close()
        #print metad
        return metad

    def _parse_creation_date(self, creation_date):
        if len(creation_date) <= 2:
            return None
        elif len(creation_date) <= 24: #Thu Jan 28 11:09:10 2016
            return datetime.strptime(creation_date,"%a %b %d %H:%M:%S %Y")
        elif len(creation_date) <= 32: #2016-01-11 11:49:50.0 -0500
            return datetime.strptime(creation_date[:-6],"%Y-%m-%d %H:%M:%S.%f")
        else: #if len(creation_date) <= 36:  #2016-01-11 11:49:50.835182735 -0500
            return datetime.strptime(creation_date[:26],"%Y-%m-%d %H:%M:%S.%f")

    def _rebuild_scene_from_dict(self, md):

        ## Loop over dict, adding attributes to scene object
        for k in md:
            setattr(self,k,md[k])

        ## Verify presence of key attributes
        for k in self.key_attribs:
            try:
                if getattr(self,k) is None:
                    raise RuntimeError("Scene object is missing key attribute: {}".format(k))
            except AttributeError as e:
                raise RuntimeError("Scene object is missing key attribute: {}".format(k))

    key_attribs = (
        'acqdate1',
        'acqdate2',
        'algm_version',
        'bands',
        'catid1',
        'catid2',
        'creation_date',
        'dem',
        'epsg',
        'id',
        'filesz_dem',
        'filesz_lsf',
        'filesz_mt',
        'filesz_or',
        'geom',
        'lsf_dem',
        'matchtag',
        'metapath',
        'ndv',
        'ortho',
        'pairname',
        'proj',
        'proj4',
        'proj4_meta',
        'res',
        'res_str',
        'sceneid',
        'sensor1',
        'sensor2',
        'srcdir',
        'srcfn',
        'srcfp',
        'srs',
        'stripid',
        'wkt_esri',
        'xres',
        'xsize',
        'yres',
        'ysize',
    )


class SetsmDem(object):

    def __init__(self, filepath, md=None):

        ## If md dictionary is passed in, recreate object from dict instead of from file location
        if md:
            self._rebuild_scene_from_dict(md)

        else:
            self.srcfp = filepath
            self.srcdir, self.srcfn = os.path.split(self.srcfp)
            self.stripid = self.srcfn[:-8]
            self.id = self.stripid
            if 'lsf' in self.srcfn:
                self.is_lsf = True
            else:
                self.is_lsf = False

            metapath = os.path.join(self.srcdir,self.stripid+"_meta.txt")
            if os.path.isfile(metapath):
                self.metapath = metapath
            else:
                self.metapath = None

            self.matchtag = os.path.join(self.srcdir,self.stripid+"_matchtag.tif")
            self.ortho = os.path.join(self.srcdir,self.stripid+"_ortho.tif")
            self.mdf = os.path.join(self.srcdir,self.stripid+"_mdf.txt")
            self.readme = os.path.join(self.srcdir,self.stripid+"_readme.txt")
            self.browse = os.path.join(self.srcdir,self.stripid+"_dem_browse.tif")
            self.density_file = os.path.join(self.srcdir,self.stripid+"_density.txt")
            self.reg_files = [
                os.path.join(self.srcdir,self.stripid+"_reg.txt"),
                os.path.join(self.srcdir,self.stripid+"_oibreg.txt"),
                os.path.join(self.srcdir,self.stripid+"_ngareg.txt")
            ]
            self.archive = os.path.join(self.srcdir,self.stripid+".tar.gz")
            #self.archive = os.path.join(self.srcdir,self.stripid+".tar")

            #### parse name
            for pattern in setsm_strip_pattern, setsm_strip_pattern2:
                match = pattern.search(self.srcfn)
                if match:
                    groups = match.groupdict()
                    self.pairname = groups['pairname']
                    self.catid1 = groups['catid1']
                    self.catid2 = groups['catid2']
                    self.acqdate1 = datetime.strptime(groups['timestamp'], '%Y%m%d') # if present, the metadata file value will overwrite this
                    self.acqdate2 = self.acqdate1
                    self.sensor1 = groups['sensor'] # if present, the metadata file value will overwrite this
                    self.sensor2 = self.sensor1
                    self.res = groups['res']
                    self.creation_date = None
                    self.algm_version = 'SETSM' # if present, the metadata file value will overwrite this
                    self.geom = None
                    if 'version' in groups:
                        self.version = groups['version']
                    else:
                        self.version = None
                    break
            if not match:
                raise RuntimeError("DEM name does not match expected pattern: {}".format(self.srcfp))

    def get_geocell(self):

        centroid = self.geom.Centroid()

        ## Convert to wgs84
        srs = utils.osr_srs_preserve_axis_order(osr.SpatialReference())
        rc = srs.ImportFromProj4(self.proj4_meta)
        if not srs_wgs84.IsSame(srs):
            ctf = osr.CoordinateTransformation(srs, srs_wgs84)
            centroid.Transform(ctf)

        lat = centroid.GetY()
        lon = centroid.GetX()
        lat_letter = 'n' if lat>=0 else 's'
        lon_letter = 'e' if lon>=0 else 'w'

        self.geocell = '{}{:02d}{}{:03d}'.format(lat_letter, int(abs(math.floor(lat))), lon_letter, int(abs(math.floor(lon))))
        return self.geocell

    def get_dem_info(self):

        ## get size of non-ortho deliverable files in GB
        try:
            self.filesz_dem = os.path.getsize(self.srcfp) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_dem = 0

        try:
            self.filesz_mt = os.path.getsize(self.matchtag) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_mt = 0

        try:
            self.filesz_or = os.path.getsize(self.ortho) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_or = 0

        ds = gdal.Open(self.srcfp)
        if ds is not None:
            self.xsize = ds.RasterXSize
            self.ysize = ds.RasterYSize
            self.proj = ds.GetProjectionRef() if ds.GetProjectionRef() != '' else ds.GetGCPProjection()
            self.gtf = ds.GetGeoTransform()

            #print raster.proj
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(self.proj)
            self.srs = src_srs
            self.proj4 = src_srs.ExportToProj4()
            #print self.proj4
            self.epsg = ''

            for epsg in epsgs:
                tgt_srs = osr.SpatialReference()
                tgt_srs.ImportFromEPSG(epsg)
                #print epsg
                #print src_srs.IsSame(tgt_srs)
                if src_srs.IsSame(tgt_srs) == 1:
                    self.epsg = epsg

            src_srs.MorphToESRI()
            self.wkt_esri = src_srs.ExportToWkt()

            self.bands = ds.RasterCount
            self.datatype = ds.GetRasterBand(1).DataType
            self.datatype_readable = gdal.GetDataTypeName(self.datatype)
            self.ndv = ds.GetRasterBand(1).GetNoDataValue()

            num_gcps = ds.GetGCPCount()

            if num_gcps == 0:

                self.xres = abs(self.gtf[1])
                self.yres = abs(self.gtf[5])
                ulx = self.gtf[0] + 0 * self.gtf[1] + 0 * self.gtf[2]
                uly = self.gtf[3] + 0 * self.gtf[4] + 0 * self.gtf[5]
                urx = self.gtf[0] + self.xsize * self.gtf[1] + 0 * self.gtf[2]
                ury = self.gtf[3] + self.xsize * self.gtf[4] + 0 * self.gtf[5]
                llx = self.gtf[0] + 0 * self.gtf[1] + self.ysize * self.gtf[2]
                lly = self.gtf[3] + 0 * self.gtf[4] + self.ysize * self.gtf[5]
                lrx = self.gtf[0] + self.xsize * self.gtf[1] + self.ysize* self.gtf[2]
                lry = self.gtf[3] + self.xsize * self.gtf[4] + self.ysize * self.gtf[5]

            elif num_gcps == 4:

                gcps = ds.GetGCPs()
                gcp_dict = {}
                id_dict = {"UpperLeft":1,
                           "1":1,
                           "UpperRight":2,
                           "2":2,
                           "LowerLeft":4,
                           "4":4,
                           "LowerRight":3,
                           "3":3}

                for gcp in gcps:
                    gcp_dict[id_dict[gcp.Id]] = [float(gcp.GCPPixel), float(gcp.GCPLine), float(gcp.GCPX), float(gcp.GCPY), float(gcp.GCPZ)]

                ulx = gcp_dict[1][2]
                uly = gcp_dict[1][3]
                urx = gcp_dict[2][2]
                ury = gcp_dict[2][3]
                llx = gcp_dict[4][2]
                lly = gcp_dict[4][3]
                lrx = gcp_dict[3][2]
                lry = gcp_dict[3][3]

                self.xres = abs(math.sqrt((ulx - urx)**2 + (uly - ury)**2)/ self.xsize)
                self.yres = abs(math.sqrt((ulx - llx)**2 + (uly - lly)**2)/ self.ysize)

        else:
            raise RuntimeError("Cannot open image: %s" %self.srcfp)

        ds = None

        self.get_metafile_info()
        self.get_geocell()

    def compute_density_and_statistics(self):
        #### If no mdf or mdf does not contain valid density key, compute
        if self.density is None or self.density == 'None':
            self.density = None

            #### If matchtag exists, get matchtag density within data boundary
            if not os.path.isfile(self.matchtag):
                raise RuntimeError("Matchtag file does not exist for DEM: {}".format(self.srcfp))
            else:
                geom_area = self.geom.Area()
                ds = gdal.Open(self.matchtag)
                b = ds.GetRasterBand(1)
                gtf = ds.GetGeoTransform()
                matchtag_res_x = gtf[1]
                matchtag_res_y = gtf[5]
                matchtag_ndv = b.GetNoDataValue()
                data = b.ReadAsArray()
                err = gdal.GetLastErrorNo()
                if err <> 0:
                    raise RuntimeError("Matchtag dataset read error: {}, {}".format(gdal.GetLastErrorMsg(),self.srcfp))
                else:
                    data_pixel_count = numpy.count_nonzero(data != matchtag_ndv)
                    data_area = abs(data_pixel_count * matchtag_res_x * matchtag_res_y)
                    #logger.info("matchtag res: x = {}, y = {}".format(matchtag_res_x,matchtag_res_y))
                    #logger.info("pixel count = {}".format(data_pixel_count))
                    #logger.info("data area = {}".format(data_area))
                    #logger.info("geom area = {}".format(geom_area))
                    self.density = data_area / geom_area
                    #logger.info("calculated matchtag density = {}".format(self.density))
                    data = None
                    ds = None

        if self.stats[0] is None or self.stats[0] == 'None':
            ds = gdal.Open(self.srcfp)
            try:
                self.stats = ds.GetRasterBand(1).GetStatistics(True,True)
            except RuntimeError, e:
                logger.warning("Cannot get stats for image: {}".format(e))
                self.stats = (None, None, None, None)

        fh = open(self.density_file, 'w')
        fh.write('{}\n'.format(self.density))
        stats_str = [str(stat) for stat in self.stats]
        fh.write('{}\n'.format(','.join(stats_str)))
        fh.close()

    def get_metafile_info(self):

        ## If metafile exists
        if self.metapath:
            metad = self._parse_metadata_file()

            self.scenes = metad['scene_list']
            self.alignment_dct = metad['alignment_dct']

            pts = zip(metad['X'].split(), metad['Y'].split())
            #### create geometry

            if pts == [('NaN', 'NaN')]:
                logger.error("No valid vertices found: {}".format(self.metapath))
                self.geom = None
            else:
                poly_vts = []
                for pt in pts:
                    poly_vts.append("{} {}".format(pt[0],pt[1]))
                if len(pts) > 0:
                    poly_vts.append("{} {}".format(pts[0][0],pts[0][1]))

                if len(poly_vts) > 0:
                    poly_wkt = 'POLYGON (( {} ))'.format(", ".join(poly_vts))
                    self.geom = ogr.CreateGeometryFromWkt(poly_wkt)

            self.proj4_meta = metad['Strip projection (proj4)'].replace("'","")

            if 'Strip creation date' in metad:
                self.creation_date = datetime.strptime(metad['Strip creation date'],"%d-%b-%Y %H:%M:%S")
            else:
                raise RuntimeError('Key "Strip creation date" not found in meta dict from {}'.format(self.metapath))

            # get version
            values = []
            for x in range(len(self.scenes)):
                if 'SETSM Version' in self.scenes[x]:
                    values.append(self.scenes[x]['SETSM Version'])
            if len(values) > 0:
                self.algm_version = 'SETSM {}'.format(values[0])

            ## get acqdates
            values = []
            for x in range(len(self.scenes)):
                if 'Image_1_Acquisition_time' in self.scenes[x]:
                    values.append(self.scenes[x]['Image_1_Acquisition_time'])
            if len(values) > 0:
                self.acqdate1 = datetime.strptime(values[0], "%Y-%m-%dT%H:%M:%S.%fZ")

            values = []
            for x in range(len(self.scenes)):
                if 'Image_2_Acquisition_time' in self.scenes[x]:
                    values.append(self.scenes[x]['Image_2_Acquisition_time'])
            if len(values) > 0:
                self.acqdate2 = datetime.strptime(values[0], "%Y-%m-%dT%H:%M:%S.%fZ")

            ## get sensors
            values = []
            for x in range(len(self.scenes)):
                if 'Image_1_satID' in self.scenes[x]:
                    values.append(self.scenes[x]['Image_1_satID'])
            if len(values) > 0:
                self.sensor1 = values[0]

            values = []
            for x in range(len(self.scenes)):
                if 'Image_2_satID' in self.scenes[x]:
                    values.append(self.scenes[x]['Image_2_satID'])
            if len(values) > 0:
                self.sensor2 = values[0]

            #### If density file exists, get density from there
            self.density = None
            self.stats = (None, None, None, None)
            if os.path.isfile(self.density_file):
                fh = open(self.density_file,'r')
                lines = fh.readlines()
                density = lines[0].strip()
                self.density = float(density)
                stats = lines[1].strip().split(',')
                try:
                    self.stats = [float(stat) for stat in stats]
                except ValueError:
                    self.stats = (None,None,None,None)
                fh.close()

            #### If reg.txt file exists, parse it for registration info
            self.reginfo_list = []

            for reg_file in self.reg_files:
                dx, dy, dz, num_gcps, mean_resid_z = [None, None, None, None, None]
                if os.path.isfile(reg_file):
                    fh = open(reg_file, 'r')
                    for line in fh.readlines():
                        if line.startswith("Translation Vector (dz,dx,dy)"):
                            vectors = line.split('=')[1].split(',')
                            dz, dx, dy = [float(v.strip()) for v in vectors]
                        elif line.startswith("Mean Vertical Residual"):
                            mean_resid_z = line.split('=')[1].strip()
                        elif line.startswith("# GCPs"):
                            num_gcps = line.split('=')[1].strip()
                    if dx is not None and num_gcps is not None and mean_resid_z is not None:
                        self.reginfo_list.append(RegInfo(dx, dy, dz, num_gcps, mean_resid_z, reg_file))
                    else:
                        logger.error("Registration file cannot be parsed: {}".format(reg_file))
                    #logger.info("dz: {}, dx: {}, dy: {}".format(self.dz, self.dx, self.dy))
                    fh.close()

        ## If mdf exists without metafile
        elif os.path.isfile(self.mdf):
            metad = self._read_mdf_file()

            ## populate attribs
            keys = metad.keys()
            x_keys = [int(k[11:]) for k in keys if k.startswith("STRIP_DEM_X") ]
            y_keys = [int(k[11:]) for k in keys if k.startswith("STRIP_DEM_Y") ]
            x_keys.sort()
            y_keys.sort()

            poly_vts = []
            for i in range(1,len(x_keys)+1):
                x = metad["STRIP_DEM_X{}".format(i)]
                y = metad["STRIP_DEM_Y{}".format(i)]
                poly_vts.append("{} {}".format(x,y))

            if len(poly_vts) > 0:
                poly_vts.append("{} {}".format(metad["STRIP_DEM_X1"],metad["STRIP_DEM_Y1"]))
                poly_wkt = 'POLYGON (( {} ))'.format(", ".join(poly_vts))
                self.geom = ogr.CreateGeometryFromWkt(poly_wkt)

            ## density, stats, proj4, creation date, version
            try:
                self.density = float(metad['STRIP_DEM_matchtagDensity'])
            except ValueError, e:
                logger.info("Cannot convert density value ({}) to float for {}".format(metad['STRIP_DEM_matchtagDensity'], self.srcfp))
                self.density = None

            try:
                min_elev = float(metad['STRIP_DEM_minElevValue'])
                max_elev = float(metad['STRIP_DEM_maxElevValue'])
            except ValueError, e:
                logger.info("Cannot convert min or max elev values (min={}, max={}) to float for {}".format(metad['STRIP_DEM_minElevValue'], metad['STRIP_DEM_maxElevValue'], self.srcfp))
                min_elev, max_elev = None, None

            self.stats = (min_elev, max_elev, None, None)
            self.proj4_meta = metad['STRIP_DEM_horizontalCoordSysProj4'].replace("'","")
            self.creation_date = datetime.strptime(metad['STRIP_DEM_stripCreationTime'], "%Y-%m-%dT%H:%M:%S.%fZ")

            try:
                self.algm_version = metad['COMPONENT_1_setsmVersion']
            except KeyError, e:
                pass

            ## acqdate
            try:
                self.acqdate1 = datetime.strptime(metad['STRIP_DEM_acqDate1'], "%Y-%m-%d")
                self.acqdate2 = datetime.strptime(metad['STRIP_DEM_acqDate2'], "%Y-%m-%d")
            except KeyError, e:
                self.acqdate1 = datetime.strptime(metad['STRIP_DEM_acqDate'], "%Y-%m-%d")
                self.acqdate2 = self.acqdate1

            ## registration info (code assumes only one registration source in mdf)
            self.reginfo_list = []
            try:
                dx = float(metad['STRIP_DEM_REGISTRATION_registrationDX'])
                dy = float(metad['STRIP_DEM_REGISTRATION_registrationDY'])
                dz = float(metad['STRIP_DEM_REGISTRATION_registrationDZ'])
                mean_resid_z = float(metad['STRIP_DEM_REGISTRATION_registrationMeanVerticalResidual'])
                num_gcps = int(metad['STRIP_DEM_REGISTRATION_registrationNumGCPs'])
                name = metad['STRIP_DEM_REGISTRATION_registrationSource']
            except KeyError, e:
                logger.warning("Registration info not found in {}".format(self.srcfp))
            else:
                self.reginfo_list.append(RegInfo(dx, dy, dz, num_gcps, mean_resid_z, None, name))

        else:
            raise RuntimeError("Neither meta.txt nor mdf.txt file exists for DEM")

    def write_mdf_file(self,lsf_flag=False):

        if self.geom:

            tm = datetime.now()

            mdf_contents1 = [
                #### product specific info
                ('generationTime',tm.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
                ('numRows',self.ysize),
                ('numColumns',self.xsize),
                ('productType','"BasicStrip"'),
                ('bitsPerPixel',32),
                ('compressionType','"LZW"'),
                ('outputFormat','"GeoTiff"'),

                #### Strip DEM info
                ('BEGIN_GROUP','STRIP_DEM'),
                ('stripDemId','"{}"'.format(self.stripid)),
                ('stripCreationTime',(self.creation_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if self.creation_date else '')),
                ('releaseVersion','"{}"'.format(self.version if self.version else 'NA')),
                ('noDataValue',self.ndv),
                ('platform1','"{}"'.format(self.sensor1)),
                ('platform2','"{}"'.format(self.sensor2)),
                ('catId1','"{}"'.format(self.catid1)),
                ('catId2','"{}"'.format(self.catid2)),
                ('acqDate1',self.acqdate1.strftime("%Y-%m-%d")),
                ('acqDate2',self.acqdate2.strftime("%Y-%m-%d")),
            ]

            #### make list of points
            pnt_list = []

            if self.geom.GetGeometryCount() <> 1:
                raise RuntimeError("Geometry has multiple parts: {}, {}".format(self.geom.ExportToWkt(),self.srcfp))

            g1 = self.geom.GetGeometryRef(0)
            for i in range(0,g1.GetPointCount()):
                pnt = g1.GetPoint(i)
                x_tuple = ('X{}'.format(i+1),pnt[0])
                y_tuple = ('Y{}'.format(i+1),pnt[1])
                pnt_list.append(x_tuple)
                pnt_list.append(y_tuple)

            mdf_contents2 = [
                ('horizontalCoordSysOGCWKT',self.proj),
                ('horizontalCoordSysESRIWKT',self.wkt_esri),
                ('horizontalCoordSysProj4',self.proj4),
                ('horizontalCoordSysEPSG',self.epsg),
                ('horizontalCoordSysUnits','"meters"'),
                ('horizontalResolution',(self.xres+self.yres)/2.0),
                ('verticalCoordSys','"WGS84 Ellipsoidal Height"'),
                ('verticalCoordSysUnits','"meters"'),
                ('minElevValue',self.stats[0]),
                ('maxElevValue',self.stats[1]),
                ('matchtagDensity',self.density),
                ('lsfApplied',str(lsf_flag))
            ]

            mdf_contents3 = []
            if len(self.reginfo_list) > 0:
                for reginfo in self.reginfo_list:
                    mdf_contents3 = mdf_contents3 + [
                        ('BEGIN_GROUP','REGISTRATION'),
                        ('registrationSource',reginfo.name),
                        ('registrationDZ',reginfo.dz),
                        ('registrationDX',reginfo.dx),
                        ('registrationDY',reginfo.dy),
                        ('registrationNumGCPs', reginfo.num_gcps),
                        ('registrationMeanVerticalResidual', reginfo.mean_resid_z),
                        ('END_GROUP','REGISTRATION'),
                    ]
            mdf_contents3 = mdf_contents3 + [('END_GROUP','STRIP_DEM')]

            scene_contents = []
            i = 0
            for scene in self.scenes:
                i +=1

                cont = [
                    ('BEGIN_GROUP','COMPONENT_{}'.format(i)),
                    ('sceneDemId','"{}"'.format(scene['scene_name']))
                ]

                if 'SETSM Version' in scene:
                    cont.append(('setsmVersion',scene['SETSM Version']))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'SETSM Version'))

                if 'Creation Date' in scene:
                    cont.append(('sceneCreationDate',self._parse_creation_date(scene['Creation Date'])))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'Creation Date'))

                if 'Image 1' in scene:
                    cont.append(('sourceImage1','"{}"'.format(os.path.splitext(os.path.basename(scene['Image 1']))[0])))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'Image 1'))

                if 'Image 2' in scene:
                    cont.append(('sourceImage2','"{}"'.format(os.path.splitext(os.path.basename(scene['Image 2']))[0])))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'Image 2'))

                if 'Output Resolution' in scene:
                    cont.append(('outputResolution',scene['Output Resolution']))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'Output Resolution'))

                #### Raise no warning if these elements are missing
                if 'RA Params' in scene:
                    cont = cont + [
                        ('RAParamX',scene['RA Params'].split()[0] if len(scene['RA Params'])>2 else ' '),
                        ('RAParamY',scene['RA Params'].split()[1] if len(scene['RA Params'])>2 else ' ')
                    ]

                if 'RA Tile #' in scene:
                    cont.append(('RATileNum',scene['RA Tile #']))

                if 'RA tilesize' in scene:
                    cont.append(('RATileSize',scene['RA tilesize']))

                if 'tilesize' in scene:
                    cont.append(('TileSize',scene['tilesize']))

                if 'Seed DEM' in scene:
                    cont.append(('seedDem','"{}"'.format(os.path.basename(scene['Seed DEM']) if len(scene['Seed DEM']) > 2 else '')))
                else:
                    logger.warning('Scene metadata missing from {}: {}, key: {}'.format(self.metapath,scene['scene_name'],'Seed DEM'))

                if scene['scene_name'] in self.alignment_dct:
                    rmse, dz, dx, dy = self.alignment_dct[scene['scene_name']]
                    cont = cont + [
                        ('BEGIN_GROUP','MOSAIC_ALIGNMENT'),
                        ('rmse',rmse),
                        ('dz',dz),
                        ('dx',dx),
                        ('dy',dy),
                        ('END_GROUP','MOSAIC_ALIGNMENT')
                    ]

                cont.append(('END_GROUP','COMPONENT_{}'.format(i)))

                scene_contents = scene_contents + cont

            mdf_contents = mdf_contents1 + pnt_list + mdf_contents2 + mdf_contents3 + scene_contents

            mdf = open(self.mdf,'w')
            text = format_as_imd(mdf_contents)
            #logger.info(self.mdf)
            #logger.info("\n"+text)
            mdf.write(text)
            mdf.close()

    def write_readme_file(self):
        #### general info
        readme_contents = [
            ('licenseText','"Acknowledgment for the SETSM surface models should be present in any publication, proceeding, presentation, etc. You must notify Ian Howat at The Ohio State University if you are to use the surface models in any of those forms. Please note, the SETSM mosaics are currently in BETA release. The dataset authors make no guarantees of product accuracy and cannot be held liable for any errors, events, etc. arising from its use."'),
            ('contact','"Polar Geospatial Center, University of Minnesota, 612-626-0505, www.pgc.umn.edu"'),
            ('BEGIN_GROUP','PRODUCT_1'),
            ('demFilename','{}'.format(self.srcfn)),
            ('metadataFilename','{}'.format(os.path.basename(self.mdf))),
            ('matchtagFilename','{}'.format(os.path.basename(self.matchtag))),
            ('browseFilename','{}'.format(os.path.basename(self.browse))),
            ('readmeFilename','{}'.format(os.path.basename(self.readme))),
            ('END_GROUP','PRODUCT_1'),
        ]

        readme = open(self.readme,'w')
        text = format_as_imd(readme_contents)
        #logger.info(text)
        readme.write(text)
        readme.close()

    def _read_mdf_file(self):
        if os.path.isfile(self.mdf):
            mdf = open(self.mdf,'r')
            mdf_dct = {}
            prefix_list = []
            #### each line is key value pair, unless "BEGIN_GROUP" or "END_GROUP" which decend/acend to/from a child dct
            for line in mdf.readlines():
                if " = " in line:
                    line = line.strip()
                    line = line.strip(';')
                    key,val = line.split(" = ")
                    val = val.strip('"')
                    if key == "BEGIN_GROUP":
                        prefix_list.append(val)
                    elif key == "END_GROUP":
                        prefix_list.pop()
                    else:
                        comp_key = "_".join(prefix_list+[key])
                        mdf_dct[comp_key] = val

            mdf.close()
            return mdf_dct
        else:
            return None

    def _parse_metadata_file(self):
        metad = {}

        mdf = open(self.metapath,'r')
        in_header = True
        scene_dict = None
        scene_list = []
        alignment_dct = {}
        for line in mdf.readlines():
            l = line.strip()

            if l:
                #print l, in_header
                #### Set scene number marker
                if l == 'Scene Metadata':
                    scene_num = 0
                    in_header = False
                elif l.startswith("scene ") and not in_header:
                    scene_num +=1
                    #print scene_dict
                    if scene_dict is not None:
                        scene_list.append(scene_dict)
                    scene_dict = {}

                #### strip metadata info
                if in_header:
                    if ': ' in l:
                        try:
                            key,val = l.split(': ')
                        except ValueError, e:
                            logger.error('Cannot split line on ": " - {}, {}, {}'.format(l,e,self.metapath))
                        else:
                            metad[key.strip()] = val.strip()

                    elif '.tif ' in l:
                        alignment_stats = l.split()
                        scene_id = os.path.splitext(alignment_stats[0])[0]
                        alignment_dct[scene_id] = alignment_stats[1:]

                #### scene metadata info
                if not in_header:
                    if '=' in l:
                        if l.startswith('Output Projection='):
                            key = 'Output Projection='
                            val = l[l.find('=')+1:]
                        else:
                            try:
                                key,val = l.split('=')
                            except ValueError, e:
                                logger.error('Cannot split line on "=" - {}, {}, {}'.format(l,e,self.metapath))
                            else:
                                if key.startswith('scene '):
                                    key = 'scene_name'
                                    scene_dict[key.strip()] = os.path.splitext(val.strip())[0]
                                else:
                                    scene_dict[key.strip()] = val.strip()

        if scene_dict is not None:
            scene_list.append(scene_dict)
        metad['scene_list'] = scene_list
        metad['alignment_dct'] = alignment_dct

        mdf.close()

        #print metad

        return metad

    def _parse_creation_date(self, creation_date):
        if len(creation_date) <= 2:
            return ''
        elif len(creation_date) <= 24: #Thu Jan 28 11:09:10 2016
            return datetime.strptime(creation_date,"%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        elif len(creation_date) <= 32: #2016-01-11 11:49:50.0 -0500
            return datetime.strptime(creation_date[:-6],"%Y-%m-%d %H:%M:%S.%f").strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else: #if len(creation_date) <= 36:  #2016-01-11 11:49:50.835182735 -0500
            return datetime.strptime(creation_date[:26],"%Y-%m-%d %H:%M:%S.%f").strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _rebuild_scene_from_dict(self, md):

        ## Loop over dict, adding attributes to scene object
        for k in md:
            setattr(self,k,md[k])

        ## Verify presence of key attributes
        for k in self.key_attribs:
            try:
                if getattr(self,k) is None:
                    raise RuntimeError("Strip object is missing key attribute: {}".format(k))
            except AttributeError as e:
                raise RuntimeError("Strip object is missing key attribute: {}".format(k))

    key_attribs = (
        'acqdate1',
        'acqdate2',
        'algm_version',
        'alignment_dct',
        'archive',
        'bands',
        'browse',
        'catid1',
        'catid2',
        'creation_date',
        'datatype',
        'datatype_readable',
        'epsg',
        'filesz_dem',
        'filesz_mt',
        'filesz_or',
        'geocell',
        'geom',
        'gtf',
        'id',
        'is_lsf',
        'matchtag',
        'mdf',
        'metapath',
        'ndv',
        'ortho',
        'pairname',
        'proj',
        'proj4',
        'proj4_meta',
        'readme',
        'reg_files',
        'reginfo_list',
        'res',
        'scenes',
        'sensor1',
        'sensor2',
        'srcdir',
        'srcfn',
        'srcfp',
        'srs',
        'stats',
        'stripid',
        'wkt_esri',
        'xres',
        'xsize',
        'yres',
        'ysize'
    )


class AspDem(object):
    def __init__(self,filepath):
        self.srcfp = filepath
        self.srcdir, self.srcfn = os.path.split(self.srcfp)
        self.stripid = self.srcfn[:-8]

        metapath = os.path.join(self.srcdir,self.stripid+".geojson")
        if os.path.isfile(metapath):
            self.metapath = metapath
        else:
            logger.warning("Source metadata file not found: {}".format(metapath))
            self.metapath = None

        self.interr = os.path.join(self.srcdir,self.stripid+"-IntersectionErr.tif")
        self.ortho = os.path.join(self.srcdir,self.stripid+"-DRG.tif")

        #### parse name
        match = asp_strip_pattern.match(self.srcfn)
        if match:
            groups = match.groupdict()
            self.pairname = groups['pairname']
            self.catid1 = groups['catid1']
            self.catid2 = groups['catid2']
            self.acqdate = datetime.strptime(groups['timestamp'],'%Y%m%d')
            self.sensor = groups['sensor']
            self.creation_date = None
            self.algm_version = 'ASP'
            self.geom = None
        else:
            raise RuntimeError("DEM name does not match expected pattern: {}".format(self.srcfp))

    def get_dem_info(self):

        ds = gdal.Open(self.srcfp)
        if ds is not None:
            self.xsize = ds.RasterXSize
            self.ysize = ds.RasterYSize
            self.proj = ds.GetProjectionRef() if ds.GetProjectionRef() != '' else ds.GetGCPProjection()
            self.gtf = ds.GetGeoTransform()

            #print raster.proj
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(self.proj)
            self.proj4 = src_srs.ExportToProj4()
            #print self.proj4
            self.epsg = ''

            for epsg in epsgs:
                tgt_srs = osr.SpatialReference()
                tgt_srs.ImportFromEPSG(epsg)
                #print epsg
                #print src_srs.IsSame(tgt_srs)
                if src_srs.IsSame(tgt_srs) == 1:
                    self.epsg = epsg

            src_srs.MorphToESRI()
            self.wkt_esri = src_srs.ExportToWkt()

            self.bands = ds.RasterCount
            self.datatype = ds.GetRasterBand(1).DataType
            self.datatype_readable = gdal.GetDataTypeName(self.datatype)
            self.ndv = ds.GetRasterBand(1).GetNoDataValue()
            try:
                self.stats = ds.GetRasterBand(1).GetStatistics(True,True)
            except RuntimeError, e:
                logger.warning("Cannot get stats for image: {}".format(e))
                self.stats = (None, None, None, None)

            num_gcps = ds.GetGCPCount()

            if num_gcps == 0:

                self.xres = abs(self.gtf[1])
                self.yres = abs(self.gtf[5])
                ulx = self.gtf[0] + 0 * self.gtf[1] + 0 * self.gtf[2]
                uly = self.gtf[3] + 0 * self.gtf[4] + 0 * self.gtf[5]
                urx = self.gtf[0] + self.xsize * self.gtf[1] + 0 * self.gtf[2]
                ury = self.gtf[3] + self.xsize * self.gtf[4] + 0 * self.gtf[5]
                llx = self.gtf[0] + 0 * self.gtf[1] + self.ysize * self.gtf[2]
                lly = self.gtf[3] + 0 * self.gtf[4] + self.ysize * self.gtf[5]
                lrx = self.gtf[0] + self.xsize * self.gtf[1] + self.ysize* self.gtf[2]
                lry = self.gtf[3] + self.xsize * self.gtf[4] + self.ysize * self.gtf[5]

            elif num_gcps == 4:

                gcps = ds.GetGCPs()
                gcp_dict = {}
                id_dict = {"UpperLeft":1,
                           "1":1,
                           "UpperRight":2,
                           "2":2,
                           "LowerLeft":4,
                           "4":4,
                           "LowerRight":3,
                           "3":3}

                for gcp in gcps:
                    gcp_dict[id_dict[gcp.Id]] = [float(gcp.GCPPixel), float(gcp.GCPLine), float(gcp.GCPX), float(gcp.GCPY), float(gcp.GCPZ)]

                ulx = gcp_dict[1][2]
                uly = gcp_dict[1][3]
                urx = gcp_dict[2][2]
                ury = gcp_dict[2][3]
                llx = gcp_dict[4][2]
                lly = gcp_dict[4][3]
                lrx = gcp_dict[3][2]
                lry = gcp_dict[3][3]

                self.xres = abs(math.sqrt((ulx - urx)**2 + (uly - ury)**2)/ self.xsize)
                self.yres = abs(math.sqrt((ulx - llx)**2 + (uly - lly)**2)/ self.ysize)

        else:
            raise RuntimeError("Cannot open image: %s" %self.srcfp)

        ds = None

        if self.metapath:
            index = self.metapath

            ds2 = ogr.Open(index)
            if ds2 is not None:
                lyr2 = ds2.GetLayer(0)
                if not lyr2:
                    logger.error("Cannot read {}".format(index))
                else:
                    lyr2.ResetReading()
                    srs = lyr2.GetSpatialRef()

                    for feat2 in lyr2:

                        #get attribs
                        for fld_def in utils.OVERLAP_FILE_ATTRIBUTE_DEFINITIONS:
                            fld = fld_def.fname
                            i = feat2.GetFieldIndex(fld)
                            if i >= 0:
                                attrib = feat2.GetField(i)
                                if attrib:
                                    if fld == "OVERLAP":
                                        overlap = attrib
                                    if fld == "DEM_NAME":
                                        dem_name = attrib


                        #### transfrom and write geom
                        self.geom = feat2.GetGeometryRef().Clone()

            ds2 = None


class SetsmTile(object):

    def __init__(self, srcfp, md=None):

        ## If md dictionary is passed in, recreate object from dict instead of from file location
        if md:
            self._rebuild_scene_from_dict(md)

        else:
            self.srcfp = srcfp
            self.srcdir, self.srcfn = os.path.split(self.srcfp)
            if 'reg' in self.srcfn:
                self.tileid = self.srcfn[:-12]
                name_base = self.srcfn[:-8]
            else:
                self.tileid = self.srcfn[:-8]
                name_base = self.tileid

            self.id = self.tileid

            self.matchtag = os.path.join(self.srcdir,name_base + '_matchtag.tif')
            self.err = os.path.join(self.srcdir,name_base + '_err.tif')
            self.day = os.path.join(self.srcdir,name_base + '_day.tif')
            self.ortho = os.path.join(self.srcdir,name_base + '_ortho.tif')
            self.density_file = os.path.join(self.srcdir,name_base + '_density.txt')
            self.count = os.path.join(self.srcdir,name_base + '_count.tif')
            self.countmt = os.path.join(self.srcdir,name_base + '_countmt.tif')
            self.mad = os.path.join(self.srcdir,name_base + '_mad.tif')
            self.mindate = os.path.join(self.srcdir,name_base + '_mindate.tif')
            self.maxdate = os.path.join(self.srcdir,name_base + '_maxdate.tif')

            self.browse = os.path.join(self.srcdir,name_base + '_dem_browse.tif')
            if not os.path.isfile(self.browse):
                self.browse = os.path.join(self.srcdir,name_base + '_browse.tif')

            self.archive = os.path.join(self.srcdir,self.tileid+".tar.gz")

            match = setsm_tile_pattern.match(self.srcfn)
            if match:
                groups = match.groupdict()
                self.tilename = groups['tile']
                self.res = groups['res']
                self.version = groups['version']
                self.subtile = groups['subtile']

                if self.subtile:
                    metabase = self.tileid.replace('_'+self.subtile,'')
                    self.metapath = os.path.join(self.srcdir,metabase + '_dem_meta.txt')
                    if not os.path.isfile(self.metapath):
                        self.metapath = os.path.join(self.srcdir, self.tileid + '_meta.txt')
                    if not os.path.isfile(self.metapath):
                        raise RuntimeError("Meta file not found for {}".format(self.srcfp))
                    self.regmetapath = os.path.join(self.srcdir, metabase + '_reg.txt')
                else:
                    self.metapath = os.path.join(self.srcdir, self.tileid + '_dem_meta.txt')
                    if not os.path.isfile(self.metapath):
                        self.metapath = os.path.join(self.srcdir, self.tileid + '_meta.txt')
                    if not os.path.isfile(self.metapath):
                        raise RuntimeError("Meta file not found for {}".format(self.srcfp))
                    self.regmetapath = os.path.join(self.srcdir, self.tileid + '_reg.txt')

                self.supertile_id = '{}_{}'.format(self.tilename,self.res)

            else:
                raise RuntimeError("DEM name does not match expected pattern: {}".format(self.srcfn))


    def get_dem_info(self):

        try:
            self.filesz_dem = os.path.getsize(self.srcfp) / 1024 / 1024 / 1024.0
        except OSError:
            self.filesz_dem = 0

        ds = gdal.Open(self.srcfp)
        if ds is not None:
            self.xsize = ds.RasterXSize
            self.ysize = ds.RasterYSize
            self.proj = ds.GetProjectionRef() if ds.GetProjectionRef() != '' else ds.GetGCPProjection()
            self.gtf = ds.GetGeoTransform()

            #print raster.proj
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(self.proj)
            self.srs = src_srs
            self.proj4 = src_srs.ExportToProj4()
            #print self.proj4
            self.epsg = ''

            for epsg in epsgs:
                tgt_srs = osr.SpatialReference()
                tgt_srs.ImportFromEPSG(epsg)
                #print epsg
                #print src_srs.IsSame(tgt_srs)
                if src_srs.IsSame(tgt_srs) == 1:
                    self.epsg = epsg

            src_srs.MorphToESRI()
            self.wkt_esri = src_srs.ExportToWkt()

            self.bands = ds.RasterCount
            self.datatype = ds.GetRasterBand(1).DataType
            self.datatype_readable = gdal.GetDataTypeName(self.datatype)
            self.ndv = ds.GetRasterBand(1).GetNoDataValue()
            try:
                self.stats = ds.GetRasterBand(1).GetStatistics(True,True)
            except RuntimeError, e:
                logger.warning("Cannot get stats for image: {}, {}".format(self.srcfp,e))
                self.stats = (None, None, None, None)

            num_gcps = ds.GetGCPCount()

            if num_gcps == 0:

                self.xres = abs(self.gtf[1])
                self.yres = abs(self.gtf[5])
                ulx = self.gtf[0] + 0 * self.gtf[1] + 0 * self.gtf[2]
                uly = self.gtf[3] + 0 * self.gtf[4] + 0 * self.gtf[5]
                urx = self.gtf[0] + self.xsize * self.gtf[1] + 0 * self.gtf[2]
                ury = self.gtf[3] + self.xsize * self.gtf[4] + 0 * self.gtf[5]
                llx = self.gtf[0] + 0 * self.gtf[1] + self.ysize * self.gtf[2]
                lly = self.gtf[3] + 0 * self.gtf[4] + self.ysize * self.gtf[5]
                lrx = self.gtf[0] + self.xsize * self.gtf[1] + self.ysize* self.gtf[2]
                lry = self.gtf[3] + self.xsize * self.gtf[4] + self.ysize * self.gtf[5]

            elif num_gcps == 4:

                gcps = ds.GetGCPs()
                gcp_dict = {}
                id_dict = {"UpperLeft":1,
                           "1":1,
                           "UpperRight":2,
                           "2":2,
                           "LowerLeft":4,
                           "4":4,
                           "LowerRight":3,
                           "3":3}

                for gcp in gcps:
                    gcp_dict[id_dict[gcp.Id]] = [float(gcp.GCPPixel), float(gcp.GCPLine), float(gcp.GCPX), float(gcp.GCPY), float(gcp.GCPZ)]

                ulx = gcp_dict[1][2]
                uly = gcp_dict[1][3]
                urx = gcp_dict[2][2]
                ury = gcp_dict[2][3]
                llx = gcp_dict[4][2]
                lly = gcp_dict[4][3]
                lrx = gcp_dict[3][2]
                lry = gcp_dict[3][3]

                self.xres = abs(math.sqrt((ulx - urx)**2 + (uly - ury)**2)/ self.xsize)
                self.yres = abs(math.sqrt((ulx - llx)**2 + (uly - lly)**2)/ self.ysize)

            poly_wkt = 'POLYGON (( %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f ))' %(ulx,uly,urx,ury,lrx,lry,llx,lly,ulx,uly)
            self.geom = ogr.CreateGeometryFromWkt(poly_wkt)

        else:
            raise RuntimeError("Cannot open image: %s" %self.srcfp)

        ds = None

        #### if metadata file parse it
        if self.metapath:
            self.get_metafile_info()

        #### If density file exists, get density from there
        self.density = None
        if os.path.isfile(self.density_file):
            fh = open(self.density_file,'r')
            lines = fh.readlines()
            density = lines[0].strip()
            self.density = float(density)
            fh.close()

    def compute_density_and_statistics(self):
        #### If no density file, compute
        if not os.path.isfile(self.density_file):
            self.density = None

            #### If dem exists, get dem density within data boundary
            geom_area = self.geom.Area()
            ds = gdal.Open(self.srcfp)
            b = ds.GetRasterBand(1)
            gtf = ds.GetGeoTransform()
            res_x = gtf[1]
            res_y = gtf[5]
            ndv = b.GetNoDataValue()
            data = b.ReadAsArray()
            err = gdal.GetLastErrorNo()
            if err <> 0:
                raise RuntimeError("DEM dataset read error: {}, {}".format(gdal.GetLastErrorMsg(),self.srcfp))
            else:
                data_pixel_count = numpy.count_nonzero(data != ndv)
                data_area = abs(data_pixel_count * res_x * res_y)
                #logger.info("matchtag res: x = {}, y = {}".format(matchtag_res_x,matchtag_res_y))
                #logger.info("pixel count = {}".format(data_pixel_count))
                #logger.info("data area = {}".format(data_area))
                #logger.info("geom area = {}".format(geom_area))
                self.density = data_area / geom_area
                #logger.info("matchtag density = {}".format(self.density))
                data = None
                ds = None

            fh = open(self.density_file, 'w')
            fh.write('{}\n'.format(self.density))
            fh.close()

    def get_geom(self):

        ds = gdal.Open(self.srcfp)
        if ds is not None:
            self.xsize = ds.RasterXSize
            self.ysize = ds.RasterYSize
            self.proj = ds.GetProjectionRef() if ds.GetProjectionRef() != '' else ds.GetGCPProjection()
            self.gtf = ds.GetGeoTransform()

            #print raster.proj
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(self.proj)
            self.srs = src_srs
            self.proj4 = src_srs.ExportToProj4()
            #print self.proj4
            self.epsg = ''

            for epsg in epsgs:
                tgt_srs = osr.SpatialReference()
                tgt_srs.ImportFromEPSG(epsg)
                #print epsg
                #print src_srs.IsSame(tgt_srs)
                if src_srs.IsSame(tgt_srs) == 1:
                    self.epsg = epsg

            src_srs.MorphToESRI()
            self.wkt_esri = src_srs.ExportToWkt()

            self.bands = ds.RasterCount
            self.datatype = ds.GetRasterBand(1).DataType
            self.datatype_readable = gdal.GetDataTypeName(self.datatype)
            self.ndv = ds.GetRasterBand(1).GetNoDataValue()

            num_gcps = ds.GetGCPCount()

            if num_gcps == 0:

                self.xres = abs(self.gtf[1])
                self.yres = abs(self.gtf[5])
                ulx = self.gtf[0] + 0 * self.gtf[1] + 0 * self.gtf[2]
                uly = self.gtf[3] + 0 * self.gtf[4] + 0 * self.gtf[5]
                urx = self.gtf[0] + self.xsize * self.gtf[1] + 0 * self.gtf[2]
                ury = self.gtf[3] + self.xsize * self.gtf[4] + 0 * self.gtf[5]
                llx = self.gtf[0] + 0 * self.gtf[1] + self.ysize * self.gtf[2]
                lly = self.gtf[3] + 0 * self.gtf[4] + self.ysize * self.gtf[5]
                lrx = self.gtf[0] + self.xsize * self.gtf[1] + self.ysize* self.gtf[2]
                lry = self.gtf[3] + self.xsize * self.gtf[4] + self.ysize * self.gtf[5]

            elif num_gcps == 4:

                gcps = ds.GetGCPs()
                gcp_dict = {}
                id_dict = {"UpperLeft":1,
                           "1":1,
                           "UpperRight":2,
                           "2":2,
                           "LowerLeft":4,
                           "4":4,
                           "LowerRight":3,
                           "3":3}

                for gcp in gcps:
                    gcp_dict[id_dict[gcp.Id]] = [float(gcp.GCPPixel), float(gcp.GCPLine), float(gcp.GCPX), float(gcp.GCPY), float(gcp.GCPZ)]

                ulx = gcp_dict[1][2]
                uly = gcp_dict[1][3]
                urx = gcp_dict[2][2]
                ury = gcp_dict[2][3]
                llx = gcp_dict[4][2]
                lly = gcp_dict[4][3]
                lrx = gcp_dict[3][2]
                lry = gcp_dict[3][3]

                self.xres = abs(math.sqrt((ulx - urx)**2 + (uly - ury)**2)/ self.xsize)
                self.yres = abs(math.sqrt((ulx - llx)**2 + (uly - lly)**2)/ self.ysize)

            poly_wkt = 'POLYGON (( %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f, %.12f %.12f ))' %(ulx,uly,urx,ury,lrx,lry,llx,lly,ulx,uly)
            self.geom = ogr.CreateGeometryFromWkt(poly_wkt)

        else:
            raise RuntimeError("Cannot open image: %s" %self.srcfp)

        ds = None

    def get_metafile_info(self):

        metad = self._parse_metadata_file()
        self.alignment_dct = metad['alignment_dct']

        if 'Creation Date' in metad:
            self.creation_date = datetime.strptime(metad['Creation Date'],"%d-%b-%Y %H:%M:%S")
        else:
            raise RuntimeError('Key "Creation Date" not found in meta dict from {}'.format(self.metapath))

        self.num_components = len(self.alignment_dct)

        self.sum_gcps = 0
        self.mean_resid_z = None
        self.reg_src = None

        if '# GCPs' in metad:
            try:
                self.num_gcps = int(sum(metad['# GCPs']))
            except ValueError, e:
                self.num_gcps = 0
        if 'Mean Vertical Residual (m)' in metad:
            residuals = [resid for resid in metad['Mean Vertical Residual (m)'] if not math.isnan(resid)]
            if len(residuals) > 0:
                self.mean_resid_z = sum(residuals) / len(residuals)
        if 'Registration Dataset 1 Name' in metad:
            reg_src = metad['Registration Dataset 1 Name']
            if reg_src in ['GLA14_rel634','GLA14_rel34','GLA06_rel531','GLA12_14_rel634_greenland_all_xyz','GLA12_14_rel634']:
                self.reg_src = 'ICESat'
            elif reg_src =='Neighbor Align':
                self.reg_src = reg_src

    def _parse_metadata_file(self):
        metad = {}

        mdf = open(self.metapath,'r')
        alignment_dct = {}
        for line in mdf.readlines():
            l = line.strip()

            if l:
                if ': ' in l:
                    try:
                        key,val = l.split(': ')
                    except ValueError, e:
                        logger.error('Cannot split line on ": " - {}, {}, {}'.format(l,e,self.metapath))
                    else:
                        metad[key.strip()] = val.strip()

                elif 'seg' in l:
                    alignment_stats = l.split()
                    scene_id = os.path.splitext(alignment_stats[0])[0]
                    alignment_dct[scene_id] = alignment_stats[1:]

        metad['alignment_dct'] = alignment_dct

        mdf.close()

        if os.path.isfile(self.regmetapath):
            mdf = open(self.regmetapath,'r')
            for line in mdf.readlines():
                l = line.strip()
                if l:
                    if ': ' in l:
                        try:
                            key,val = l.split(': ')
                        except ValueError, e:
                            logger.error('Cannot split line on ": " - {}, {}, {}'.format(l,e,self.metapath))
                        else:
                            if val:
                                metad[key.strip()] = val.strip()
                    elif l.startswith(('Mean Vertical Residual','# GCPs')):
                        key,val = l.split('=')
                        if val.strip():
                            if key.strip() in metad:
                                metad[key.strip()].append(float(val.strip()))
                            else:
                                metad[key.strip()] = [float(val.strip())]
        mdf.close()
        #print metad

        return metad

    def _rebuild_scene_from_dict(self, md):

        ## Loop over dict, adding attributes to scene object
        for k in md:
            setattr(self,k,md[k])

        ## Verify presence of key attributes
        for k in self.key_attribs:
            try:
                if getattr(self,k) is None:
                    raise RuntimeError("Tile object is missing key attribute: {}".format(k))
            except AttributeError as e:
                raise RuntimeError("Tile object is missing key attribute: {}".format(k))

    key_attribs = (
        'alignment_dct',
        'archive',
        'bands',
        'browse',
        'creation_date',
        'datatype',
        'datatype_readable',
        'day',
        'epsg',
        'err',
        'filesz_dem',
        'geom',
        'gtf',
        'id',
        'matchtag',
        'mean_resid_z',
        'metapath',
        'ndv',
        'num_components',
        'num_gcps',
        'ortho',
        'proj',
        'proj4',
        'reg_src',
        'regmetapath',
        'res',
        'srcdir',
        'srcfn',
        'srcfp',
        'srs',
        'stats',
        'sum_gcps',
        'tileid',
        'tilename',
        'wkt_esri',
        'xres',
        'xsize',
        'yres',
        'ysize',
    )

class RegInfo(object):

    def __init__(self, dx, dy, dz, num_gcps, mean_resid_z, src, name=None):
        self.src = src

        if name:
            self.name = name

        else:
            self.name = 'Unknown'
            if src.endswith('oibreg.txt'):
                self.name = 'IceBridge'
            elif src.endswith('ngareg.txt'):
                self.name = 'NGA'
            elif src.endswith('reg.txt'):
                self.name = 'ICESat'

        self.dx = dx
        self.dy = dy
        self.dz = dz
        self.num_gcps = num_gcps
        self.mean_resid_z = mean_resid_z


def format_as_imd(contents):
    text = ''
    tab_count = 0
    tab_offset = 0
    for key,val in contents:
        if key == 'BEGIN_GROUP':
            tab_count +=1
            tab_offset = -1
            eol = '\n'
        elif key == 'END_GROUP':
            tab_count -=1
            tab_offset = 0
            eol = '\n'
        else:
            tab_offset = 0
            eol = ';\n'

        text = text + '{}{} = {}{}'.format('\t'*(tab_count+tab_offset),key,val,eol)
    text = text + 'END;'
    return text
