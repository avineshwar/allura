import logging
from datetime import datetime, timedelta

from tg import config
from pylons import c, g, request
import pkg_resources
from webob import exc
from bson import ObjectId

from ming import schema as S
from ming.utils import LazyProperty
from ming.orm import ThreadLocalORMSession
from ming.orm.base import mapper, session, state
from ming.orm.mapped_class import MappedClass
from ming.orm.property import FieldProperty, RelationProperty, ForeignIdProperty

from allura.lib import helpers as h
from allura.lib import plugin
from allura.lib import exceptions
from allura.lib import security

from .session import main_orm_session
from .session import project_doc_session, project_orm_session
from .neighborhood import Neighborhood
from .auth import ProjectRole
from .types import ACL, ACE

from filesystem import File

log = logging.getLogger(__name__)

class ProjectFile(File):
    class __mongometa__:
        session = main_orm_session

    project_id=FieldProperty(S.ObjectId)
    category=FieldProperty(str)
    caption=FieldProperty(str)

class ProjectCategory(MappedClass):
    class __mongometa__:
        session = main_orm_session
        name='project_category'

    _id=FieldProperty(S.ObjectId)
    parent_id = FieldProperty(S.ObjectId, if_missing=None)
    name=FieldProperty(str)
    label=FieldProperty(str, if_missing='')
    description=FieldProperty(str, if_missing='')

    @property
    def parent_category(self):
        return self.query.get(_id=self.parent_id)

    @property
    def subcategories(self):
        return self.query.find(dict(parent_id=self._id)).all()

class Project(MappedClass):
    _perms_base = [ 'read', 'update', 'admin', 'create']
    _perms_init = _perms_base + [ 'register' ]
    class __mongometa__:
        session = main_orm_session
        name='project'
        indexes = [
            'name',
            'neighborhood_id',
            ('neighborhood_id', 'name'),
            'shortname' ]

    # Project schema
    _id=FieldProperty(S.ObjectId)
    parent_id = FieldProperty(S.ObjectId, if_missing=None)
    neighborhood_id = ForeignIdProperty(Neighborhood)
    shortname = FieldProperty(str)
    name=FieldProperty(str)
    show_download_button=FieldProperty(bool, if_missing=True)
    short_description=FieldProperty(str, if_missing='')
    description=FieldProperty(str, if_missing='')
    homepage_title=FieldProperty(str, if_missing='')
    external_homepage=FieldProperty(str, if_missing='')
    support_page=FieldProperty(str, if_missing='')
    support_page_url=FieldProperty(str, if_missing='')
    removal=FieldProperty(str, if_missing='')
    moved_to_url=FieldProperty(str, if_missing='')
    removal_changed_date = FieldProperty(datetime, if_missing=datetime.utcnow)
    export_controlled=FieldProperty(bool, if_missing=False)
    database=FieldProperty(S.Deprecated)
    database_uri=FieldProperty(str)
    is_root=FieldProperty(bool)
    acl = FieldProperty(ACL(permissions=_perms_init))
    neighborhood_invitations=FieldProperty([S.ObjectId])
    neighborhood = RelationProperty(Neighborhood)
    app_configs = RelationProperty('AppConfig')
    category_id = FieldProperty(S.ObjectId, if_missing=None)
    deleted = FieldProperty(bool, if_missing=False)
    labels = FieldProperty([str])
    last_updated = FieldProperty(datetime, if_missing=None)
    tool_data = FieldProperty({str:{str:None}}) # entry point: prefs dict
    ordinal = FieldProperty(int, if_missing=0)
    database_configured = FieldProperty(bool, if_missing=True)
    _extra_tool_status = FieldProperty([str])

    @property
    def permissions(self):
        if self.shortname == '--init--':
            return self._perms_init
        else:
            return self._perms_base

    def parent_security_context(self):
        '''ACL processing should proceed up the project hierarchy.'''
        return self.parent_project

    @classmethod
    def default_database_uri(cls, shortname):
        base = config.get('ming.project.master')
        db = config.get('ming.project.database')
        return base + '/' + db

    @LazyProperty
    def allowed_tool_status(self):
        return ['production'] + self._extra_tool_status

    @h.exceptionless([], log)
    def sidebar_menu(self):
        from allura.app import SitemapEntry
        result = []
        if not self.is_root:
            p = self.parent_project
            result.append(SitemapEntry('Parent Project'))
            result.append(SitemapEntry(p.name or p.script_name, p.script_name))
        sps = self.direct_subprojects
        if sps:
            result.append(SitemapEntry('Child Projects'))
            result += [
                SitemapEntry(p.name or p.script_name, p.script_name)
                for p in sps ]
        return result

    def get_tool_data(self, tool, key, default=None):
        return self.tool_data.get(tool, {}).get(key, None)

    def set_tool_data(self, tool, **kw):
        d = self.tool_data.setdefault(tool, {})
        d.update(kw)
        state(self).soil()

    def admin_menu(self):
        return []

    @property
    def script_name(self):
        url = self.url()
        if '//' in url:
            return url.rsplit('//')[-1]
        else:
            return url

    def url(self):
        if self.shortname.endswith('--init--'):
            return self.neighborhood.url()
        shortname = self.shortname[len(self.neighborhood.shortname_prefix):]
        url = self.neighborhood.url_prefix + shortname + '/'
        if url.startswith('//'):
            try:
                return request.scheme + ':' + url
            except TypeError: # pragma no cover
                return 'http:' + url
        else:
            return url

    def best_download_url(self):
        provider = plugin.ProjectRegistrationProvider.get()
        return provider.best_download_url(self)

    def get_screenshots(self):
        return ProjectFile.query.find(dict(
                project_id=self._id,
                category='screenshot')).all()

    @property
    def icon(self):
        return ProjectFile.query.get(
            project_id=self._id,
            category='icon')

    @property
    def description_html(self):
        return g.markdown.convert(self.description)

    @property
    def parent_project(self):
        if self.is_root: return None
        return self.query.get(_id=self.parent_id)

    def private_project_of(self):
        '''
        If this is a user-project, return the User, else None
        '''
        user = None
        if self.shortname.startswith('u/'):
            from .auth import User
            user = User.query.get(username=self.shortname[2:])
        return user

    @LazyProperty
    def root_project(self):
        if self.is_root: return self
        return self.parent_project.root_project

    @LazyProperty
    def project_hierarchy(self):
        if not self.is_root:
            return self.root_project.project_hierarchy
        projects = set([self])
        while True:
            new_projects = set(
                self.query.find(dict(parent_id={'$in':[p._id for p in projects]})))
            new_projects.update(projects)
            if new_projects == projects: break
            projects = new_projects
        return projects

    @property
    def category(self):
        return ProjectCategory.query.find(dict(_id=self.category_id)).first()

    def roleids_with_permission(self, name):
        roles = set()
        for p in self.parent_iter():
            for ace in p.acl:
                if ace.permission == name and ace.access == ACE.alllow:
                    roles.add(ace.role_id)
        return list(roles)

    def sitemap(self):
        from allura.app import SitemapEntry
        sitemap = SitemapEntry('root')
        entries = []
        for sub in self.direct_subprojects:
            if sub.deleted: continue
            entries.append({'ordinal':sub.ordinal,'entry':SitemapEntry(sub.name, sub.url())})
        for ac in self.app_configs:
            App = ac.load()
            app = App(self, ac)
            if app.is_visible_to(c.user):
                for sm in app.sitemap:
                    entry = sm.bind_app(app)
                    entry.ui_icon='tool-%s' % ac.tool_name
                    ordinal = 'ordinal' in ac.options and ac.options['ordinal'] or 0
                    entries.append({'ordinal':ordinal,'entry':entry})
        entries = sorted(entries, key=lambda e: e['ordinal'])
        for e in entries:
            sitemap.children.append(e['entry'])
        return sitemap.children

    def parent_iter(self):
        yield self
        pp = self.parent_project
        if pp:
            for p in pp.parent_iter():
                yield p

    @property
    def subprojects(self):
        q = self.query.find(dict(shortname={'$gt':self.shortname})).sort('shortname')
        for project in q:
            if project.shortname.startswith(self.shortname + '/'):
                yield project
            else:
                break

    @property
    def direct_subprojects(self):
        return self.query.find(dict(parent_id=self._id))

    @property
    def accolades(self):
        from .artifact import AwardGrant
        return AwardGrant.query.find(dict(granted_to_project_id=self._id)).all()

    @property
    def named_roles(self):
        roles = sorted(
            g.credentials.project_roles(self.root_project._id).named,
            key=lambda r:r.name.lower())
        return roles

    def install_app(self, ep_name, mount_point=None, mount_label=None, ordinal=0, **override_options):
        for ep in pkg_resources.iter_entry_points('allura', ep_name):
            App = ep.load()
            break
        else:
            # Try case-insensitive install
            for ep in pkg_resources.iter_entry_points('allura'):
                if ep.name.lower() == ep_name:
                    App = ep.load()
                    break
            else: # pragma no cover
                raise exc.HTTPNotFound, ep_name
        if not mount_point:
            base_mount_point = mount_point = App.default_mount_point
            for x in range(10):
                if self.app_instance(mount_point) is None: break
                mount_point = base_mount_point + '-%d' % x
        if not h.re_path_portion.match(mount_point):
            raise exceptions.ToolError, 'Mount point "%s" is invalid' % mount_point
        if self.app_instance(mount_point) is not None:
            raise exceptions.ToolError, (
                'Mount point "%s" is already in use' % mount_point)
        assert self.app_instance(mount_point) is None
        options = App.default_options()
        options['mount_point'] = mount_point
        options['mount_label'] = mount_label or App.default_mount_label or mount_point
        options['ordinal'] = ordinal
        options.update(override_options)
        h.log_action(log, 'install tool').info(
            'install tool %s', ep_name,
            meta=dict(tool_type=ep_name, mount_point=options['mount_point'], mount_label=options['mount_label']))
        cfg = AppConfig(
            project_id=self._id,
            tool_name=ep.name,
            options=options)
        app = App(self, cfg)
        with h.push_config(c, project=self, app=app):
            session(cfg).flush()
            app.install(self)
        return app

    def uninstall_app(self, mount_point):
        app = self.app_instance(mount_point)
        if app is None: return
        if self.support_page == app.config.options.mount_point:
            self.support_page = ''
        with h.push_config(c, project=self, app=app):
            app.uninstall(self)

    def app_instance(self, mount_point_or_config):
        if isinstance(mount_point_or_config, AppConfig):
            app_config = mount_point_or_config
        else:
            app_config = self.app_config(mount_point_or_config)
        if app_config is None:
            return None
        App = app_config.load()
        if App is None: # pragma no cover
            return None
        else:
            return App(self, app_config)

    def app_config(self, mount_point):
        return AppConfig.query.find({
                'project_id':self._id,
                'options.mount_point':mount_point}).first()

    def new_subproject(self, name, install_apps=True, user=None):
        if not h.re_path_portion.match(name):
            raise exceptions.ToolError, 'Mount point "%s" is invalid' % name
        provider = plugin.ProjectRegistrationProvider.get()
        return provider.register_subproject(self, name, user or c.user, install_apps)

    def delete(self):
        # Cascade to subprojects
        for sp in self.direct_subprojects:
            sp.delete()
        # Cascade to app configs
        for ac in self.app_configs:
            ac.delete()
        MappedClass.delete(self)

    def render_widget(self, widget):
        app = self.app_instance(widget['mount_point'])
        with h.push_config(c, project=self, app=app):
            return getattr(app.widget(app), widget['widget_name'])()

    def breadcrumbs(self):
        entry = ( self.name, self.url() )
        if self.parent_project:
            return self.parent_project.breadcrumbs() + [ entry ]
        else:
            return [ (self.neighborhood.name, self.neighborhood.url())] + [ entry ]

    def users(self):
        '''Find all the users who have named roles for this project'''
        named_roles = security.RoleCache(
            g.credentials,
            g.credentials.project_roles(project_id=self.root_project._id).named)
        return [ r.user for r in named_roles.roles_that_reach if r.user_id is not None ]

    def user_in_project(self, username):
        from .auth import User
        u = User.by_username(username)
        named_roles = g.credentials.project_roles(project_id=self.root_project._id).named
        for r in named_roles.roles_that_reach:
            if r.user_id == u._id: return u
        return None

    def configure_project(
        self,
        users=None, apps=None, is_user_project=False):
        from allura import model as M
        if users is None: users = [ c.user ]
        if apps is None:
            if is_user_project:
                apps = [('profile', 'profile'),
                        ('admin', 'admin'),
                        ('search', 'search')]
            else:
                apps = [('home', 'home'),
                        ('admin', 'admin'),
                        ('search', 'search')]
        with h.push_config(c, project=self, user=users[0]):
            # Install default named roles (#78)
            root_project_id=self.root_project._id
            role_admin = M.ProjectRole.upsert(name='Admin', project_id=root_project_id)
            role_developer = M.ProjectRole.upsert(name='Developer', project_id=root_project_id)
            role_member = M.ProjectRole.upsert(name='Member', project_id=root_project_id)
            role_auth = M.ProjectRole.upsert(name='*authenticated', project_id=root_project_id)
            role_anon = M.ProjectRole.upsert(name='*anonymous', project_id=root_project_id)
            # Setup subroles
            role_admin.roles = [ role_developer._id ]
            role_developer.roles = [ role_member._id ]
            self.acl = [
                ACE.allow(role_developer._id, 'read'),
                ACE.allow(role_member._id, 'read'),
                ACE.allow(role_anon._id, 'read')]
            self.acl += [
                M.ACE.allow(role_admin._id, perm)
                for perm in self.permissions ]
            for user in users:
                pr = user.project_role()
                pr.roles = [ role_admin._id, role_developer._id, role_member._id ]
            # Setup apps
            for ep_name, mount_point in apps:
                self.install_app(ep_name, mount_point)
            self.database_configured = True
            ThreadLocalORMSession.flush_all()

    def add_user(self, user, role_names):
        'Convenience method to add member with the given role(s).'
        pr = user.project_role(self)
        for role_name in role_names:
            r = ProjectRole.by_name(role_name, self)
            pr.roles.append(r._id)

class AppConfig(MappedClass):
    """
    Configuration information for an instantiated :class:`Application <allura.app.Application>`
    in a project

    :var options: an object on which various options are stored.  options.mount_point is the url component for this app instance
    :var acl: a dict that maps permissions (strings) to lists of roles that have the permission
    """

    class __mongometa__:
        session = project_orm_session
        name='config'

    # AppConfig schema
    _id=FieldProperty(S.ObjectId)
    project_id=ForeignIdProperty(Project)
    discussion_id=ForeignIdProperty('Discussion')
    tool_name=FieldProperty(str)
    version=FieldProperty(str)
    options=FieldProperty(None)
    project = RelationProperty(Project, via='project_id')
    discussion = RelationProperty('Discussion', via='discussion_id')

    acl = FieldProperty(ACL())

    def parent_security_context(self):
        '''ACL processing should terminate at the AppConfig'''
        return None

    def load(self):
        """
        :returns: the related :class:`Application <allura.app.Application>` class
        """
        try:
            result = self._loaded_ep
        except AttributeError:
            for ep in pkg_resources.iter_entry_points(
                'allura', self.tool_name):
                break
            else:
                return None
            result = self._loaded_ep = ep.load()
        return result

    def script_name(self):
        return self.project.script_name + self.options.mount_point + '/'

    def url(self):
        return self.project.url() + self.options.mount_point + '/'

    def breadcrumbs(self):
        return self.project.breadcrumbs() + [
            (self.options.mount_point, self.url()) ]
