#-*- python -*-
import logging
import re
import os
import sys
import shutil
from subprocess import Popen
from itertools import islice
from datetime import datetime
from urllib import urlencode

# Non-stdlib imports
import pkg_resources
from tg import expose, validate, redirect, response
from tg.decorators import with_trailing_slash, without_trailing_slash
import pylons
from pylons import g, c, request
from formencode import validators
from pymongo import bson
from webob import exc

from ming.orm.base import mapper
from pymongo.bson import ObjectId

# Pyforge-specific imports
from pyforge.app import Application, ConfigOption, SitemapEntry
from pyforge.lib.helpers import push_config, DateTimeConverter, mixin_reactors
from pyforge.lib.search import search
from pyforge.lib.decorators import audit, react
from pyforge.lib.security import require, has_artifact_access
from pyforge.model import ProjectRole, User, ArtifactReference, Feed

# Local imports
from forgegit import model
from forgegit import version
from .widgets import GitRevisionWidget
from .reactors import reactors

log = logging.getLogger(__name__)

class W(object):
    revision_widget = GitRevisionWidget()

class ForgeGitApp(Application):
    '''This is the Git app for PyForge'''
    __version__ = version.__version__
    permissions = [ 'read', 'write', 'create', 'admin' ]

    def __init__(self, project, config):
        Application.__init__(self, project, config)
        self.root = RootController()

    @property
    def sitemap(self):
        menu_id = self.config.options.mount_point.title()
        with push_config(c, app=self):
            return [
                SitemapEntry(menu_id, '.')[self.sidebar_menu()] ]

    def admin_menu(self):
        return []

    def sidebar_menu(self):
        links = [ SitemapEntry('Home',c.app.url, ui_icon='home') ]
        repo = c.app.repo
        if repo and repo.status == 'ready':
            branches= [ b.name for b in repo.branches ]
            tags = [ t.name for t in repo.repo_tags() ]
            if branches:
                links.append(SitemapEntry('Branches'))
                for b in branches:
                    links.append(SitemapEntry(
                            b, c.app.url + '?' + urlencode(dict(branch=b)),
                            className='nav_child'))
            if tags:
                links.append(SitemapEntry('Tags'))
                for b in tags:
                    links.append(SitemapEntry(
                            b, c.app.url + '?' + urlencode(dict(branch=b)),
                            className='nav_child'))
        return links

    @property
    def repo(self):
        return model.GitRepository.query.get(app_config_id=self.config._id)

    @property
    def templates(self):
         return pkg_resources.resource_filename('forgegit', 'templates')

    def install(self, project):
        'Set up any default permissions and roles here'
        self.config.options['project_name'] = project.name
        super(ForgeGitApp, self).install(project)
        # Setup permissions
        role_developer = ProjectRole.query.get(name='Developer')._id
        role_auth = ProjectRole.query.get(name='*authenticated')._id
        self.config.acl.update(
            read=c.project.acl['read'],
            create=[role_developer],
            write=[role_developer],
            admin=c.project.acl['tool'])
        repo = model.GitRepository()
        repo.name = self.config.options.mount_point + '.git'
        repo.fs_path = '/git/' + c.project.shortname + '/'
        repo.url_path = '/' + c.project.shortname + '/'
        repo.tool = 'git'
        repo.status = 'initing'
        g.publish('audit', 'scm.git.init', dict(repo_name=repo.name, repo_path=repo.fs_path))


    def uninstall(self, project):
        g.publish('audit', 'scm.git.uninstall', dict(project_id=project._id))

    @audit('scm.git.uninstall')
    def _uninstall(self, routing_key, data):
        "Remove all the tool's artifacts and the physical repository"
        repo = self.repo
        if repo is not None:
            shutil.rmtree(repo.full_fs_path, ignore_errors=True)
        model.GitRepository.query.remove(dict(app_config_id=self.config._id))
        super(ForgeGitApp, self).uninstall(project_id=data['project_id'])

class RootController(object):

    @expose('forgegit.templates.index')
    def index(self, offset=0, branch='master'):
        offset=int(offset)
        repo = c.app.repo
        if repo and c.app.repo.status=='ready':
            revisions = list(islice(repo.log(branch), offset, offset+10))
        else: # pragma no cover
            revisions = []
        c.revision_widget=W.revision_widget
        next_link='?' + urlencode(dict(offset=offset+10, branch=branch))
        return dict(repo=repo,
                    branch=branch,
                    host=request.host,
                    revisions=revisions,
                    next_link=next_link,
                    offset=offset)

    @expose()
    def _lookup(self, hash, *remainder):
        return CommitController(hash), remainder

class CommitController(object):

    def __init__(self, hash):
        self._hash = hash

    @expose('forgegit.templates.commit')
    def index(self):
        commit = c.app.repo.revision(rev=self._hash)
        c.revision_widget=W.revision_widget
        return dict(commit=commit)

mixin_reactors(ForgeGitApp, reactors)
