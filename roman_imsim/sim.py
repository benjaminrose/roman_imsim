# from __future__ import division
# from __future__ import print_function

# from future import standard_library
# standard_library.install_aliases()
# from builtins import str
# from builtins import range
# from past.builtins import basestring
# from builtins import object
# from past.utils import old_div

import numpy as np
import healpy as hp
import sys, os, io
import math
import copy
import logging
import time
import yaml
import copy
import galsim as galsim
import galsim.roman as roman
import galsim.config.process as process
import galsim.des as des
# import ngmix
import fitsio as fio
import pickle as pickle
import pickletools
from astropy.time import Time
#from mpi4py import MPI
# from mpi_pool import MPIPool
import cProfile, pstats, psutil
import glob
import shutil
import h5py
import io
#import guppy

from .output import accumulate_output_disk
from .image import draw_image 
from .image import draw_detector 
from .detector import modify_image
from .universe import init_catalogs
from .universe import setupCCM_ab
from .universe import addDust
from .telescope import pointing 
from .misc import ParamError
from .misc import except_func
from .misc import save_obj
from .misc import load_obj
from .misc import convert_dither_to_fits
from .misc import convert_gaia
from .misc import convert_galaxia
from .misc import create_radec_fits
from .misc import hsm
from .misc import get_filename
from .misc import get_filenames
from .misc import write_fits

# Converts galsim Roman filter names to indices in Chris' dither file.
filter_dither_dict = {
    'J129' : 3,
    'F184' : 1,
    'Y106' : 4,
    'H158' : 2
}

class roman_sim(object):
    """
    Roman image simulation.

    Input:
    param_file : File path for input yaml config file or yaml dict. Example located at: ./example.yaml.
    """

    def __init__(self, param_file, index=None):

        if isinstance(param_file, str):
            # Load parameter file
            self.params     = yaml.load(open(param_file))
            self.param_file = param_file
            # Do some parsing
            for key in list(self.params.keys()):
                if self.params[key]=='None':
                    self.params[key]=None
                if self.params[key]=='none':
                    self.params[key]=None
                if self.params[key]=='True':
                    self.params[key]=True
                if self.params[key]=='False':
                    self.params[key]=False
            if 'condor' not in self.params:
                self.params['condor']=False

        else:
            # Else use existing param dict
            self.params     = param_file


        self.index = index
        if 'tmpdir' in self.params:
            os.chdir(self.params['tmpdir'])

        # Set up some information on processes and MPI
        if self.params['mpi']:
            from mpi4py import MPI
            self.comm = MPI.COMM_WORLD
            self.rank = self.comm.Get_rank()
            self.size = self.comm.Get_size()
            print('doing mpi')
        else:
            self.comm = None
            self.rank = 0
            self.size = 1

        print('mpi',self.rank,self.size)

        # Set up logger. I don't really use this, but it could be used.
        logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
        self.logger = logging.getLogger('roman_sim')

        return

    def setup(self,filter_,dither,sca=1,setup=False,load_cats=True):
        """
        Set up initial objects.

        Input:
        filter_ : A filter name. 'None' to determine by dither.
        """
        filter_dither_dict = {
                             'J129' : 3,
                             'F184' : 1,
                             'Y106' : 4,
                             'H158' : 2}
        filter_flux_dict = {
                            'J129' : 'j_Roman',
                            'F184' : 'F184W_Roman',
                            'Y106' : 'y_Roman',
                            'H158' : 'h_Roman'}

        if filter_!='None':
            # Filter be present in filter_dither_dict{} (exists in survey strategy file).
            if filter_ not in list(filter_dither_dict.keys()):
                raise ParamError('Supplied invalid filter: '+filter_)

        # This sets up a mostly-unspecified pointing object in this filter. We will later specify a dither and SCA to complete building the pointing information.
        if filter_=='None':
            self.pointing = pointing(self.params,self.logger,filter_=None,sca=None,dither=None,rank=self.rank)
        else:
            self.pointing = pointing(self.params,self.logger,filter_=filter_,sca=None,dither=None,rank=self.rank)

        if not setup:
            # This updates the dither
            self.pointing.update_dither(dither)
            # This sets up a specific pointing for this SCA (things like WCS, PSF)
            self.pointing.update_sca(sca)

        self.gal_rng = galsim.UniformDeviate(self.params['random_seed'])
        # This checks whether a truth galaxy/star catalog exist. If it doesn't exist, it is created based on specifications in the yaml file. It then sets up links to the truth catalogs on disk.
        if load_cats:
            self.cats     = init_catalogs(self.params, self.pointing, self.gal_rng, self.rank, self.size, comm=self.comm, setup=setup)

        print('Done with init_catalogs')

        if setup:
            return False

        if load_cats:
            if len(self.cats.gal_ind)==0:
                print('skipping due to no objects near pointing',str(self.rank))
                return True

        return False

    def get_sca_list(self):
        """
        Generate list of SCAs to simulate based on input parameter file.
        """

        if hasattr(self.params,'sca'):
            if self.params['sca'] is None:
                sca_list = np.arange(1,19)
            elif self.params['sca'] == 'None':
                sca_list = np.arange(1,19)
            elif hasattr(self.params['sca'],'__len__'):
                if type(self.params['sca'])==str:
                    raise ParamError('Provided SCA list is not numeric.')
                sca_list = self.params['sca']
            else:
                sca_list = [self.params['sca']]
        else:
            sca_list = np.arange(1,19)

        return sca_list

    def get_inds(self):
        """
        Checks things are setup, cut out objects not near SCA, and distributes objects across procs.
        """

        # If something went wrong and there's no pointing defined, then crash.
        if not hasattr(self,'pointing'):
            raise ParamError('Sim object has no pointing - need to run sim.setup() first.')
        if self.pointing.dither is None:
            raise ParamError('Sim pointing object has no dither assigned - need to run sim.pointing.update_dither() first.')

        mask_sca      = self.pointing.in_sca(self.cats.gals['ra'][:],self.cats.gals['dec'][:])
        mask_sca_star = self.pointing.in_sca(self.cats.stars['ra'][:],self.cats.stars['dec'][:])
        if self.cats.supernovae is not None:
            mask_sca_supernova = self.pointing.in_sca(self.cats.supernovae['ra'][:],self.cats.supernovae['dec'][:])
        self.cats.add_mask(mask_sca,star_mask=mask_sca_star,supernova_mask=mask_sca_supernova)

    def iterate_image(self):
        """
        This is the main simulation. It instantiates the draw_image object, then iterates over all galaxies and stars. The output is then accumulated from other processes (if mpi is enabled), and saved to disk.
        """

        # Build file name path for stampe dictionary pickle
        if 'tmpdir' in self.params:
            filename = get_filename(self.params['tmpdir'],
                                    '',
                                    self.params['output_meds'],
                                    var=self.pointing.filter+'_'+str(self.pointing.dither),
                                    name2=str(self.pointing.sca)+'_'+str(self.rank),
                                    ftype='fits',
                                    overwrite=True)
            filename_ = get_filename(self.params['out_path'],
                                    'stamps',
                                    self.params['output_meds'],
                                    var=self.pointing.filter+'_'+str(self.pointing.dither),
                                    name2=str(self.pointing.sca)+'_'+str(self.rank),
                                    ftype='fits',
                                    overwrite=True)
            supernova_filename = get_filename(self.params['tmpdir'],
                                          '',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_supernova',
                                          ftype='cPickle',
                                          overwrite=True)
            supernova_filename_ = get_filename(self.params['out_path'],
                                          'stamps',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_supernova',
                                          ftype='cPickle',
                                          overwrite=True)
            star_filename = get_filename(self.params['tmpdir'],
                                          '',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_star',
                                          ftype='fits',
                                          overwrite=True)
            star_filename_ = get_filename(self.params['out_path'],
                                          'stamps',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_star',
                                          ftype='fits',
                                          overwrite=True)
        else:
            filename = get_filename(self.params['out_path'],
                                    'stamps',
                                    self.params['output_meds'],
                                    var=self.pointing.filter+'_'+str(self.pointing.dither),
                                    name2=str(self.pointing.sca)+'_'+str(self.rank),
                                    ftype='fits',
                                    overwrite=True)
            filename_ = None

            supernova_filename = get_filename(self.params['out_path'],
                                          'stamps',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_supernova',
                                          ftype='cPickle',
                                          overwrite=True)
            supernova_filename_ = None
            
            star_filename = get_filename(self.params['out_path'],
                                          'stamps',
                                          self.params['output_meds'],
                                          var=self.pointing.filter+'_'+str(self.pointing.dither),
                                          name2=str(self.pointing.sca)+'_'+str(self.rank)+'_star',
                                          ftype='fits',
                                          overwrite=True)
            star_filename_ = None

        # Instantiate draw_image object. The input parameters, pointing object, modify_image object, truth catalog object, random number generator, logger, and galaxy & star indices are passed.
        # Instantiation defines some parameters, iterables, and image bounds, and creates an empty SCA image.
        self.draw_image = draw_image(self.params, self.pointing, self.modify_image, self.cats,  self.logger, rank=self.rank, comm=self.comm)

        t0 = time.time()
        t1 = time.time()
        fits_length = 200000000
        index_table = None
        if self.cats.get_gal_length()!=0:#&(self.cats.get_star_length()==0):
            tmp,tmp_ = self.cats.get_gal_list()
            if len(tmp)!=0:
                # Build indexing table for MEDS making later
                index_table = np.zeros(50000,dtype=[('ind',int), ('sca','i8'), ('dither','i8'), ('x',float), ('y',float), ('ra',float), ('dec',float), ('mag',float), ('stamp','i8'), ('xmin','i8'), ('xmax','i8'), ('ymin','i8'), ('ymax','i8'), ('dudx',float), ('dudy',float), ('dvdx',float), ('dvdy',float), ('start_row',int)])
                index_table['ind']=-999
                # Objects to simulate
                fits = fio.FITS(filename,'rw',clobber=True)
                fits.write(np.zeros(100),extname='image_cutouts')
                fits.write(np.zeros(100),extname='weight_cutouts')
                fits['image_cutouts'].write(np.zeros(1),start=[fits_length])
                fits['weight_cutouts'].write(np.zeros(1),start=[fits_length])
                i=0
                start_row = 0
                # gals = {}
                # Empty storage dictionary for postage stamp information
                print('Attempting to simulate '+str(len(tmp))+' galaxies for SCA '+str(self.pointing.sca)+' and dither '+str(self.pointing.dither)+'.')
                gal_list = tmp
                while True:
                    # Loop over all galaxies near pointing and attempt to simulate them.
                    g_ = None
                    self.draw_image.iterate_gal()
                    # print('sim',self.draw_image.ind,self.draw_image.gal_stamp_too_large)
                    if self.draw_image.gal_done:
                        break
                    # Store postage stamp output in dictionary
                    g_ = self.draw_image.retrieve_stamp()
                    #print(g_)
                    if g_ is not None:
                        # gals[self.draw_image.ind] = g_
                        #print(type(self.params['skip_stamps']),self.params['skip_stamps'])
                        index_table['ind'][i]    = g_['ind']
                        index_table['x'][i]      = g_['x']
                        index_table['y'][i]      = g_['y']
                        index_table['ra'][i]     = g_['ra']
                        index_table['dec'][i]    = g_['dec']
                        index_table['mag'][i]    = g_['mag']
                        index_table['sca'][i]    = self.pointing.sca
                        index_table['dither'][i] = self.pointing.dither
                        if g_['gal'] is not None:
                            # print('.....yes',g_['ind'])
                            index_table['stamp'][i]  = g_['stamp']
                            index_table['start_row'][i]  = start_row
                            index_table['xmin'][i]   = g_['gal'].bounds.xmin
                            index_table['xmax'][i]   = g_['gal'].bounds.xmax
                            index_table['ymin'][i]   = g_['gal'].bounds.ymin
                            index_table['ymax'][i]   = g_['gal'].bounds.ymax
                            jac = g_['gal'].wcs.jacobian(galsim.PositionD(g_['x'],g_['y']))
                            index_table['dudx'][i]   = jac.dudx
                            index_table['dvdx'][i]   = jac.dvdx
                            index_table['dudy'][i]   = jac.dudy
                            index_table['dvdy'][i]   = jac.dvdy
                            if fits_length-start_row<256**2*2:
                                fits['image_cutouts'].write(np.zeros(1),start=[fits_length+256**2*100])
                                fits['weight_cutouts'].write(np.zeros(1),start=[fits_length+256**2*100])
                                fits_length+=256**2*100
                            fits['image_cutouts'].write(g_['gal'].array.flatten(),start=[start_row])
                            fits['weight_cutouts'].write(g_['weight'].flatten(),start=[start_row])
                            start_row += g_['stamp']**2
                        
                        i+=1
                        # if i%1000==0:
                        #     print('time',time.time()-t1)
                        #     t1 = time.time()
                        # g_.clear()

                index_table = index_table[:i]
                if 'skip_stamps' in self.params:
                    if self.params['skip_stamps']:
                        os.remove(filename)
                fits.close()
        print('galaxy time', time.time()-t0)
        # pickle.dump_session('/hpc/group/cosmology/session.pkl')


        t1 = time.time()
        index_table_star = None
        tmp,tmp_ = self.cats.get_star_list()
        if len(tmp)!=0:
            index_table_star = np.zeros(500,dtype=[('ind',int), ('sca','i8'), ('dither','i8'), ('x',float), ('y',float), ('ra',float), ('dec',float), ('mag',float), ('stamp','i8'), ('xmin','i8'), ('xmax','i8'), ('ymin','i8'), ('ymax','i8'), ('dudx',float), ('dudy',float), ('dvdx',float), ('dvdy',float), ('start_row',int)])
            index_table_star['ind']=-999
            fits = fio.FITS(star_filename,'rw',clobber=True)
            fits.write(np.zeros(100),extname='image_cutouts')
            fits.write(np.zeros(100),extname='weight_cutouts')
            fits['image_cutouts'].write(np.zeros(1),start=[6553600])
            fits['weight_cutouts'].write(np.zeros(1),start=[6553600])
            print('Attempting to simulate '+str(len(tmp))+' stars for SCA '+str(self.pointing.sca)+' and dither '+str(self.pointing.dither)+'.')
            i=0
            start_row = 0
            while True:
                # Loop over all stars near pointing and attempt to simulate them. Stars aren't saved in postage stamp form.
                self.draw_image.iterate_star()
                if self.draw_image.star_done:
                    break
                s_ = self.draw_image.retrieve_star_stamp()
                if s_ is not None:
                    index_table_star['ind'][i]    = s_['ind']
                    index_table_star['x'][i]      = s_['x']
                    index_table_star['y'][i]      = s_['y']
                    index_table_star['ra'][i]     = s_['ra']
                    index_table_star['dec'][i]    = s_['dec']
                    index_table_star['mag'][i]    = s_['mag']
                    index_table_star['sca'][i]    = self.pointing.sca
                    index_table_star['dither'][i] = self.pointing.dither
                    if s_['star'] is not None:
                        # print('.....yes',s_['ind'])
                        index_table_star['stamp'][i]  = s_['stamp']
                        index_table_star['start_row'][i]  = start_row
                        index_table_star['xmin'][i]   = s_['star'].bounds.xmin
                        index_table_star['xmax'][i]   = s_['star'].bounds.xmax
                        index_table_star['ymin'][i]   = s_['star'].bounds.ymin
                        index_table_star['ymax'][i]   = s_['star'].bounds.ymax
                        jac = s_['star'].wcs.jacobian(galsim.PositionD(s_['x'],s_['y']))
                        index_table_star['dudx'][i]   = jac.dudx
                        index_table_star['dvdx'][i]   = jac.dvdx
                        index_table_star['dudy'][i]   = jac.dudy
                        index_table_star['dvdy'][i]   = jac.dvdy
                        fits['image_cutouts'].write(s_['star'].array.flatten(),start=[start_row])
                        fits['weight_cutouts'].write(s_['weight'].flatten(),start=[start_row])
                        start_row += s_['stamp']**2
                    i+=1
            index_table_star = index_table_star[:i]
            fits.close()
        print('star time', time.time()-t1)

        index_table_sn = None
        if self.cats.supernovae is not None:
            tmp,tmp_ = self.cats.get_supernova_list()
            if tmp is not None:
                if len(tmp)!=0:
                    with io.open(supernova_filename, 'wb') as f :
                        pickler = pickle.Pickler(f)
                        index_table_sn = np.empty(int(self.cats.get_supernova_length()),dtype=[('ind',int), ('sca',int), ('dither',int), ('x',float), ('y',float), ('ra',float), ('dec',float), ('mag',float), ('hostid',int)])
                        index_table_sn['ind']=-999
                        print('Attempting to simulate '+str(len(tmp))+' supernovae for SCA '+str(self.pointing.sca)+' and dither '+str(self.pointing.dither)+'.')
                        i=0
                        while True:
                            # Loop over all supernovae near pointing and attempt to simulate them.
                            self.draw_image.iterate_supernova()
                            if self.draw_image.supernova_done:
                                break
                            s_ = self.draw_image.retrieve_supernova_stamp()
                            if s_ is not None:
                                pickler.dump(s_)
                                index_table_sn['ind'][i]    = s_['ind']
                                index_table_sn['x'][i]      = s_['x']
                                index_table_sn['y'][i]      = s_['y']
                                index_table_sn['ra'][i]     = s_['ra']
                                index_table_sn['dec'][i]    = s_['dec']
                                index_table_sn['mag'][i]    = s_['mag']
                                index_table_sn['sca'][i]    = self.pointing.sca
                                index_table_sn['dither'][i] = self.pointing.dither
                                index_table_sn['hostid'][i] = s_['hostid']
                                i+=1
                                s_.clear()
                        index_table_sn = index_table_sn[:i]

        if self.comm is not None:
            self.comm.Barrier()

        if os.path.exists(filename):
            os.system('gzip '+filename)
            if filename_ is not None:
                shutil.copy(filename+'.gz',filename_+'.gz')
                os.remove(filename+'.gz')
        if os.path.exists(star_filename):
            os.system('gzip '+star_filename)
            if star_filename_ is not None:
                shutil.copy(star_filename+'.gz',star_filename_+'.gz')
                os.remove(star_filename+'.gz')
        if os.path.exists(supernova_filename):
            os.system('gzip '+supernova_filename)
            if supernova_filename_ is not None:
                shutil.copy(supernova_filename+'.gz',supernova_filename_+'.gz')
                os.remove(supernova_filename+'.gz')
        if self.rank == 0:
            # Build file name path for SCA image
            filename = get_filename(self.params['out_path'],
                                    'images',
                                    self.params['output_meds'],
                                    var=self.pointing.filter+'_'+str(self.pointing.dither),
                                    name2=str(self.pointing.sca),
                                    ftype='fits.gz',
                                    overwrite=True)

        if self.comm is None:

            if (self.cats.get_gal_length()==0) and (len(gal_list)==0):
                return

            # No mpi, so just finalize the drawing of the SCA image and write it to a fits file.
            print('Saving SCA image to '+filename)
            write_fits(filename,self.draw_image.im,None,None,self.pointing.sca,self.params['output_meds'])
            #img = self.draw_image.finalize_sca()
            #write_fits(filename,img)

        else:
            self.comm.Barrier()
            print(self.rank,self.comm,flush=True)

            # Send/receive all versions of SCA images across procs and sum them, then finalize and write to fits file.
            if self.rank == 0:
                for i in range(1,self.size):
                    self.draw_image.im = self.draw_image.im + self.comm.recv(source=i)

                if index_table is not None:
                    print('Saving SCA image to '+filename)
                    # self.draw_image.im.write(filename+'_raw.fits.gz')
                    write_fits(filename,self.draw_image.im,None,None,self.pointing.sca,self.params['output_meds'])

            else:

                self.comm.send(self.draw_image.im, dest=0)

            # Send/receive all parts of postage stamp dictionary across procs and merge them.
            # if self.rank == 0:

            #     for i in range(1,self.size):
            #         gals.update( self.comm.recv(source=i) )

            #     # Build file name path for stampe dictionary pickle
            #     filename = get_filename(self.params['out_path'],
            #                             'stamps',
            #                             self.params['output_meds'],
            #                             var=self.pointing.filter+'_'+str(self.pointing.dither),
            #                             name2=str(self.pointing.sca),
            #                             ftype='cPickle',
            #                             overwrite=True)

            #     if gals!={}:
            #         # Save stamp dictionary pickle
            #         print('Saving stamp dict to '+filename)
            #         save_obj(gals, filename )

            # else:

            #     self.comm.send(gals, dest=0)

        if self.rank == 0:

            filename = get_filename(self.params['out_path'],
                                    'truth',
                                    self.params['output_meds'],
                                    var='index',
                                    name2=self.pointing.filter+'_'+str(self.pointing.dither)+'_'+str(self.pointing.sca),
                                    ftype='fits',
                                    overwrite=True)
            filename_star = get_filename(self.params['out_path'],
                                    'truth',
                                    self.params['output_meds'],
                                    var='index',
                                    name2=self.pointing.filter+'_'+str(self.pointing.dither)+'_'+str(self.pointing.sca)+'_star',
                                    ftype='fits',
                                    overwrite=True)
            filename_sn = get_filename(self.params['out_path'],
                                    'truth',
                                    self.params['output_meds'],
                                    var='index',
                                    name2=self.pointing.filter+'_'+str(self.pointing.dither)+'_'+str(self.pointing.sca)+'_sn',
                                    ftype='fits',
                                    overwrite=True)  

            print('before index')
            for i in range(1,self.size):
                tmp = self.comm.recv(source=i)
                if tmp is not None:
                    index_table = np.append(index_table,tmp)
            for i in range(1,self.size):
                tmp = self.comm.recv(source=i)
                if tmp is not None:
                    index_table_star = np.append(index_table_star,tmp)
            for i in range(1,self.size):
                tmp = self.comm.recv(source=i)
                if tmp is not None:
                    index_table_sn = np.append(index_table_sn,tmp)

            if index_table is not None:
                print('Saving index to '+filename)
                fio.write(filename,index_table)
            else: 
                print('Not saving index, no objects in SCA')
            if index_table_star is not None:
                fio.write(filename_star,index_table_star)
            if index_table_sn is not None:
                fio.write(filename_sn,index_table_sn)
        else:
            self.comm.send(index_table, dest=0)            
            self.comm.send(index_table_star, dest=0)            
            self.comm.send(index_table_sn, dest=0)            

    def iterate_detector_image(self):
        """
        Apply detector physics to image.
        """

        # Build file name path for SCA image
        imfilename = get_filename(self.params['out_path'],
                                'images',
                                self.params['output_meds'],
                                var=self.pointing.filter+'_'+str(self.pointing.dither),
                                name2=str(self.pointing.sca),
                                ftype='fits.gz',
                                overwrite=False)
        im = fio.FITS(imfilename)['SCI'].read()
        imfilename = get_filename(self.params['out_path'],
                                'images/'+self.modify_image.get_path_name(),
                                self.params['output_meds'],
                                var=self.pointing.filter+'_'+str(self.pointing.dither),
                                name2=str(self.pointing.sca),
                                ftype='fits.gz',
                                overwrite=True)

        self.draw_image = draw_detector(self.params, self.pointing, self.modify_image,  self.logger, rank=self.rank, comm=self.comm, im=im)
        rng = galsim.BaseDeviate(self.params['random_seed'])
        self.modify_image.setup_sky(self.draw_image.im,self.pointing,rng)
        img,err,dq,sky_mean,sky_noise = self.draw_image.finalize_sca()
        write_fits(imfilename,img,sky_noise,dq,self.pointing.sca,self.params['output_meds'],sky_mean=sky_mean)
        print('done image detector stuff')

    def iterate_detector_stamps(self,obj_type):
        """
        Apply detector physics to image.
        """

        if obj_type=='gal':
            obj_str = ''
        elif obj_type=='star':
            obj_str = '_star'
        else:
            raise ParamError('Supplied invalid obj type: '+obj_type)

        # Build file name path for stamps 
        if 'tmpdir' not in self.params:
            self.params['tmpdir'] = os.getcwd()

        filename_index = get_filename(self.params['tmpdir'],
                                '',
                                self.params['output_meds'],
                                var='index',
                                name2=self.pointing.filter+'_'+str(self.pointing.dither)+'_'+str(self.pointing.sca)+obj_str,
                                ftype='fits',
                                overwrite=True)
        filename_index_ = get_filename(self.params['out_path'],
                                'truth',
                                self.params['output_meds'],
                                var='index',
                                name2=self.pointing.filter+'_'+str(self.pointing.dither)+'_'+str(self.pointing.sca)+obj_str,
                                ftype='fits',
                                overwrite=False)
        filename = get_filename(self.params['tmpdir'],
                                '',
                                self.params['output_meds'],
                                var=self.pointing.filter+'_'+str(self.pointing.dither),
                                name2=str(self.pointing.sca)+'_'+str(self.rank)+obj_str,
                                ftype='fits',
                                overwrite=True)
        filename_ = get_filename(self.params['out_path'],
                                'stamps',
                                self.params['output_meds'],
                                var=self.pointing.filter+'_'+str(self.pointing.dither),
                                name2=str(self.pointing.sca)+'_'+str(self.rank)+obj_str,
                                ftype='fits',
                                overwrite=False)

        if os.path.exists(filename_+'.gz'):
            shutil.copy(filename_+'.gz',filename+'.gz')
            if os.path.exists(filename):
                os.remove(filename)
            os.system('gunzip '+filename+'.gz')
        else:
            raise ParamError('Could not find stamp file.')

        if os.path.exists(filename_index_):
            shutil.copy(filename_index_,filename_index)
        else:
            raise ParamError('Could not find index file.')

        self.fits_index = fio.FITS(filename_index)[-1]
        self.fits       = fio.FITS(filename,'rw')

        self.fits.write(np.zeros(100),extname='dq_cutouts')
        self.fits['dq_cutouts'].write(np.zeros(1),start=[self.fits['image_cutouts'].read_header()['NAXIS1']-1])

        for i in range(self.fits_index.read_header()['NAXIS2']):
            im,err = self.read_stamp(i)
            if im is None:
                continue

            img,err,dq,sky_mean = self.draw_image.finalize_stamp(self.fits_index[i]['ind'],self.fits_index[i]['dither'],im,err)
            start_row = self.fits_index[i]['start_row']
            self.fits['image_cutouts'].write(im.array.flatten()-sky_mean,start=[start_row])
            self.fits['weight_cutouts'].write(err.flatten(),start=[start_row])
            self.fits['dq_cutouts'].write(dq.flatten(),start=[start_row])

        os.system('gzip '+filename+'.gz')
        shutil.copy(filename+'.gz',filename_.replace('stamps','stamps/'+self.modify_image.get_path_name())+'.gz')
        os.remove(filename+'.gz')
        os.remove(filename_index)

    def check_file(self,sca,dither,filter_):
        self.pointing = pointing(self.params,self.logger,filter_=None,sca=None,dither=int(dither),rank=self.rank)
        print(sca,dither,filter_)
        f = get_filename(self.params['out_path'],
                                    'truth',
                                    self.params['output_meds'],
                                    var='index',
                                    name2=self.pointing.filter+'_'+str(dither)+'_'+str(sca),
                                    ftype='fits',
                                    overwrite=False)
        print(f)
        return os.path.exists(f)

    def read_stamp(self,i):

        if self.fits_index[i]['stamp']>0:
            start = self.fits_index[i]['start_row']
            stamp = self.fits_index[i]['stamp']
            im = galsim.Image(  self.fits['image_cutouts'][start:start+stamp**2].reshape((stamp,stamp)),
                                xmin=self.fits_index[i]['xmin'],
                                ymin=self.fits_index[i]['ymin'],
                                wcs=galsim.JacobianWCS( self.fits_index[i]['dudx'], 
                                                        self.fits_index[i]['dudy'], 
                                                        self.fits_index[i]['dvdx'], 
                                                        self.fits_index[i]['dvdy'])
                                )
            err = galsim.Image(  self.fits['weight_cutouts'][start:start+stamp**2].reshape((stamp,stamp)),
                                xmin=self.fits_index[i]['xmin'],
                                ymin=self.fits_index[i]['ymin'],
                                wcs=galsim.JacobianWCS( self.fits_index[i]['dudx'], 
                                                        self.fits_index[i]['dudy'], 
                                                        self.fits_index[i]['dvdx'], 
                                                        self.fits_index[i]['dvdy'])
                                )
            return im,err
        else:
            return None,None
