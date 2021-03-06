import logging
import os
import urlparse
import hashlib
from collections import defaultdict
from gettext import gettext as _

from debpkgr import aptrepo
from nectar.request import DownloadRequest
from pulp.plugins.util import misc, publish_step
from pulp.common.error_codes import Error
from pulp.server.exceptions import PulpCodedTaskFailedException

from pulp_deb.common import constants, ids
from pulp_deb.plugins.db import models

_logger = logging.getLogger(__name__)


DEBSYNC001 = Error(
    "DEBSYNC001",
    "Unable to sync %(repo_id)s from %(feed_url)s:"
    " expected one comp, got %(comp_count)s",
    ["repo_id", "feed_url", "comp_count"])

DEBSYNC002 = Error(
    "DEBSYNC002",
    "Unable to sync %(repo_id)s from %(feed_url)s: mismatching checksums"
    " for %(filename)s: expected %(checksum_expected)s,"
    " actual %(checksum_actual)s",
    ["repo_id", "feed_url", "filename", "checksum_expected", "checksum_actual"])


class RepoSync(publish_step.PluginStep):
    Type_Class_Map = {
        models.DebPackage.TYPE_ID: models.DebPackage,
    }

    def __init__(self, repo, conduit, config):
        """
        :param repo:        repository to sync
        :type  repo:        pulp.plugins.model.Repository
        :param conduit:     sync conduit to use
        :type  conduit:     pulp.plugins.conduits.repo_sync.RepoSyncConduit
        :param config:      config object for the sync
        :type  config:      pulp.plugins.config.PluginCallConfiguration
        """
        super(RepoSync, self).__init__(step_type=constants.SYNC_STEP,
                                       repo=repo,
                                       conduit=conduit,
                                       config=config)
        self.description = _('Syncing Repository')

        self.feed_url = self.get_config().get('feed').strip('/')
        self.releases = self.get_config().get('releases', 'stable').split(',')
        self.architectures = split_or_none(self.get_config().get('architectures'))
        self.components = split_or_none(self.get_config().get('components'))

        self.unit_relative_urls = {}
        self.available_units = None
        # dicts with release names as keys to multiplex variables
        self.apt_repo_meta = {}
        self.release_units = {}
        self.release_files = {
            release: os.path.join(self.get_working_dir(), release, 'Release')
            for release in self.releases}
        self.feed_urls = {
            release: urlparse.urljoin(self.feed_url + '/', '/'.join(['dists', release]))
            for release in self.releases}
        self.release_urls = {
            release: urlparse.urljoin(self.feed_urls[release] + '/', 'Release')
            for release in self.releases}
        self.packages_urls = {}
        # double dicts with release/component as keys
        self.component_units = defaultdict(dict)
        self.component_packages = defaultdict(dict)

        for release in self.releases:
            misc.mkdir(os.path.dirname(self.release_files[release]))
            _logger.info("Downloading %s", self.release_urls[release])

        # defining lifecycle
        #  metadata
        self.add_child(publish_step.DownloadStep(
            constants.SYNC_STEP_RELEASE_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving metadata: release file(s)'),
            downloads=[
                DownloadRequest(self.release_urls[release], self.release_files[release])
                for release in self.releases]))

        self.add_child(ParseReleaseStep(constants.SYNC_STEP_RELEASE_PARSE))

        self.step_download_Packages = publish_step.DownloadStep(
            constants.SYNC_STEP_PACKAGES_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving metadata: Packages files'))
        self.add_child(self.step_download_Packages)

        self.add_child(ParsePackagesStep(constants.SYNC_STEP_PACKAGES_PARSE))

        #  packages
        self.step_local_units = publish_step.GetLocalUnitsStep(
            importer_type=ids.TYPE_ID_IMPORTER)
        self.add_child(self.step_local_units)

        self.add_child(CreateRequestsUnitsToDownload(
            constants.SYNC_STEP_UNITS_DOWNLOAD_REQUESTS))

        self.step_download_units = publish_step.DownloadStep(
            constants.SYNC_STEP_UNITS_DOWNLOAD,
            plugin_type=ids.TYPE_ID_IMPORTER,
            description=_('Retrieving units'))
        self.add_child(self.step_download_units)

        self.add_child(SaveDownloadedUnits(constants.SYNC_STEP_SAVE))

        #  metadata
        self.add_child(SaveMetadataStep(constants.SYNC_STEP_SAVE_META))


class ParseReleaseStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(ParseReleaseStep, self).__init__(*args, **kwargs)
        self.description = _('Parse Release Files')
    def process_main(self, item=None):
        releases = self.parent.releases
        components = self.parent.components
        architectures = self.parent.architectures
        dl_reqs = []
        for release in releases:
            self.parent.apt_repo_meta[release] = repometa = aptrepo.AptRepoMeta(
                release=open(self.parent.release_files[release], "rb"),
                upstream_url=self.parent.feed_urls[release])
            # get release unit
            codename = repometa.codename
            suite = repometa.release.get('suite')
            rel_unit = self.parent.release_units[release] = models.DebRelease.\
                get_or_create_and_associate(self.parent.repo, codename, suite)
            # get release component units
            for component in repometa.components:
                if components is None or component in components:
                    self.parent.component_units[release][component] = \
                        models.DebComponent.get_or_create_and_associate(self.parent.repo,
                                                                        rel_unit,
                                                                        component)
                    self.parent.component_packages[release][component] = []
            # generate download requests for all relevant packages files
            rel_dl_reqs = repometa.create_Packages_download_requests(
                self.get_working_dir())
            # Filter the rel_dl_reqs by selected components and architectures
            if components:
                rel_dl_reqs = [
                    dlr for dlr in rel_dl_reqs
                    if dlr.data['component'] in components]
            if architectures:
                rel_dl_reqs = [
                    dlr for dlr in rel_dl_reqs
                    if dlr.data['architecture'] in architectures]
            self.parent.packages_urls[release] = set([dlr.url for dlr in rel_dl_reqs])
            dl_reqs.extend(rel_dl_reqs)
        self.parent.step_download_Packages._downloads = [
            DownloadRequest(dlr.url, dlr.destination, data=dlr.data)
            for dlr in dl_reqs]


class ParsePackagesStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(ParsePackagesStep, self).__init__(*args, **kwargs)
        self.description = _('Parse Packages Files')
    def process_main(self, item=None):
        releases = self.parent.releases
        dl_reqs = self.parent.step_download_Packages.downloads
        units = {}
        for release in releases:
            repometa = self.parent.apt_repo_meta[release]
            repometa.validate_component_arch_packages_downloads(
                [dlr for dlr in dl_reqs
                    if dlr.url in self.parent.packages_urls[release]])
            for ca in repometa.iter_component_arch_binaries():
                for pkg in ca.iter_packages():
                    pkg['checksumtype'] = 'sha256'
                    pkg['checksum'] = pkg['SHA256']
                    self.parent.unit_relative_urls[pkg['checksum']] = pkg['Filename']
                    if pkg['checksum'] in units:
                        unit = units[pkg['checksum']]
                    else:
                        unit = models.DebPackage.from_metadata(pkg)
                        units[pkg['checksum']] = unit
                    self.parent.component_packages[release][ca.component].append(unit.unit_key)
        self.parent.step_local_units.available_units = units.values()


class CreateRequestsUnitsToDownload(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(CreateRequestsUnitsToDownload, self).__init__(*args, **kwargs)
        self.description = _('Prepare Package Download')
    def process_main(self, item=None):
        wdir = os.path.join(self.get_working_dir())
        reqs = []

        feed_url = self.parent.feed_url

        step_download_units = self.parent.step_download_units
        step_download_units.path_to_unit = dict()
        dirs_to_create = set()

        for unit in self.parent.step_local_units.units_to_download:
            url = os.path.join(feed_url, self.parent.unit_relative_urls[unit.checksum])
            filename = os.path.basename(url)
            dest_dir = os.path.join(wdir, "packages", generate_internal_storage_path(filename))
            dirs_to_create.add(dest_dir)
            dest = os.path.join(dest_dir, filename)
            reqs.append(DownloadRequest(url, dest))
            step_download_units.path_to_unit[dest] = unit

        for dest_dir in dirs_to_create:
            misc.mkdir(dest_dir)
        step_download_units._downloads = reqs


class SaveDownloadedUnits(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(SaveDownloadedUnits, self).__init__(*args, **kwargs)
        self.description = _('Save and associate downloaded units')
    def process_main(self, item=None):
        path_to_unit = self.parent.step_download_units.path_to_unit
        repo = self.get_repo().repo_obj
        for path, unit in sorted(path_to_unit.items()):
            # Verify checksum first
            with open(path, "rb") as fobj:
                csum = unit._compute_checksum(fobj)
            if csum != unit.checksum:
                raise PulpCodedTaskFailedException(
                    DEBSYNC002, repo_id=self.get_repo().repo_obj.repo_id,
                    feed_url=self.parent.feed_url,
                    filename=os.path.basename(path),
                    checksum_expected=unit.checksum,
                    checksum_actual=csum)
            unit.save_and_associate(path, repo)


class SaveMetadataStep(publish_step.PluginStep):
    def __init__(self, *args, **kwargs):
        super(SaveMetadataStep, self).__init__(*args, **kwargs)
        self.description = _('Save metadata')
    def process_main(self, item=None):
        for release in self.parent.releases:
            for comp, comp_unit in self.parent.component_units[release].iteritems():
                comp_unit_packages_set = set(comp_unit.packages)
                for unit in [unit_key_to_unit(unit_key)
                             for unit_key in self.parent.component_packages[release][comp]]:
                    comp_unit_packages_set.add(unit.id)
                comp_unit.packages = list(comp_unit_packages_set)
                comp_unit.save()


def unit_key_to_unit(unit_key):
    return models.DebPackage.objects.filter(**unit_key).first()


def generate_internal_storage_path(filename):
    """
    Generate the internal storage directory for a given deb filename

    :param filename: base filename of the unit
    :type filename: str

    :returns str: The relative directory path for storing the unit
    """
    hasher = hashlib.md5()
    hasher.update(filename)
    hash_digest = hasher.hexdigest()
    part1 = hash_digest[0:2]
    part2 = hash_digest[2:4]
    storage_path = os.path.join(part1, part2)
    return storage_path


def split_or_none(data):
    if data:
        return data.split(',')
    return None
