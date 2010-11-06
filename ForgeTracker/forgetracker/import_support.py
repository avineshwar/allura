#-*- python -*-
import logging
import json, urllib, re
from datetime import datetime, timedelta
from urllib import urlencode
from webob import exc
from cStringIO import StringIO

# Non-stdlib imports
import pkg_resources
from tg import expose, validate, redirect, flash
from tg.decorators import with_trailing_slash, without_trailing_slash
from pylons import g, c, request, response
from formencode import validators
from pymongo.bson import ObjectId

from ming.orm.ormsession import ThreadLocalORMSession
from ming.orm import session, state

# Pyforge-specific imports
from allura import model as M
from allura.lib import helpers as h
from allura.app import Application, SitemapEntry, DefaultAdminController
from allura.lib.search import search_artifact
from allura.lib.decorators import audit, react
from allura.lib.security import require, has_artifact_access
from allura.lib import widgets as w
from allura.lib.widgets import form_fields as ffw
from allura.lib.widgets.subscriptions import SubscribeForm
from allura.controllers import AppDiscussionController, AppDiscussionRestController
from allura.controllers import attachments as ac
from allura.controllers import BaseController

# Local imports
from forgetracker import model as TM
from forgetracker import version

from forgetracker.widgets.ticket_form import TicketForm, TicketCustomField
from forgetracker.widgets.bin_form import BinForm
from forgetracker.widgets.ticket_search import TicketSearchResults, MassEdit, MassEditForm
from forgetracker.widgets.admin_custom_fields import TrackerFieldAdmin, TrackerFieldDisplay

log = logging.getLogger(__name__)


class ResettableStream(object):
    '''Class supporting seeks within a header of otherwise
    unseekable stream.'''

    # Seeks are supported with header of this size
    HEADER_BUF_SIZE = 2048

    def __init__(self, fp, header_size=-1):
        self.fp = fp
        self.buf = None
        self.buf_size = header_size if header_size >= 0 else self.HEADER_BUF_SIZE
        self.buf_pos = 0
        self.stream_pos = 0
        
    def read(self, size=-1):
        if self.buf is None:
            data = self.fp.read(self.buf_size)
            self.buf = StringIO(data)
            self.buf_len = len(data)
            self.stream_pos = self.buf_len
        
        data = ''
        if self.buf_pos < self.stream_pos:
            data = self.buf.read(size)
            self.buf_pos += len(data)
            if len(data) == size:
                return data
            size -= len(data)

        data += self.fp.read(size)
        self.stream_pos += len(data)
        return data
        
    def seek(self, pos):
        if self.stream_pos > self.buf_len:
            assert False, 'Started reading stream body, cannot reset pos'
        self.buf.seek(pos)
        self.buf_pos = pos
        
    def tell(self):
        if self.buf_pos < self.stream_pos:
            return self.buf_pos
        else:
            return self.stream_pos

class ImportSupport(object):

    ATTACHMENT_SIZE_LIMIT = 1024*1024

    def __init__(self):
        # At first the idea to use Ticket introspection comes,
        # but it contains various internal fields, so we'd need
        # to define somethig explicitly anyway.
        #
        # Map JSON interchange format fields to Ticket fields
        # key is JSON's field name, value is:
        #   None - drop
        #   True - map as is
        #   (new_field_name, value_convertor(val)) - use new field name and convert JSON's value
        #   handler(ticket, field, val) - arbitrary transform, expected to modify ticket in-place
        self.FIELD_MAP = {
            'assigned_to': ('assigned_to_id', self.get_user_id),
            'class': None,
            'date': ('created_date', self.parse_date), 
            'date_updated': ('mod_date', self.parse_date),
            'description': True,
            'id': None,
            'keywords': ('labels', lambda s: s.split()), # default way of handling, see below
            'status': True,
            'submitter': ('reported_by_id', self.get_user_id),
            'summary': True,
        }
        self.warnings = []
        self.errors = []
        self.options = {}
        
    def init_options(self, post_params):
        self.options = post_params
        opt_keywords = self.option('keywords_as', 'split_labels')
        if opt_keywords == 'single_label':
            self.FIELD_MAP['keywords'] = ('labels', lambda s: [s])
        elif opt_keywords == 'custom':
            del self.FIELD_MAP['keywords']

    def option(self, name, default=None):
        return self.options.get('option_' + name, False)

    @staticmethod
    def parse_date(date_string):
        return datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def get_user_id(username):
        u = M.User.by_username(username)
        if u:
            return u._id
        return None

    def custom(self, ticket, field, value):
        field = '_' + field
        if not c.app.has_custom_field(field):
            log.warning('Custom field %s is not defined, defining as string', field)
            c.app.add_custom_field(dict(name=field, label=field[1:].capitalize(), type='string'))
            ThreadLocalORMSession.flush_all()
        if 'custom_fields' not in ticket:
            ticket['custom_fields'] = {}
        ticket['custom_fields'][field] = value

    def make_artifact(self, ticket_dict):
        remapped = {}
        for f, v in ticket_dict.iteritems():
            if f in self.FIELD_MAP:
                transform = self.FIELD_MAP[f]
                if transform is None:
                    continue
                elif transform is True:
                    remapped[f] = v
                elif callable(transform):
                    transform(remapped, f, v)
                else:
                    new_f, conv = transform
                    remapped[new_f] = conv(v)
            else:
                self.custom(remapped, f, v)

        ticket = TM.Ticket(
            app_config_id=c.app.config._id,
            custom_fields=dict(),
            ticket_num=c.app.globals.next_ticket_num())
        ticket.update(remapped)
        return ticket

    def make_comment(self, thread, comment_dict):
        ts = self.parse_date(comment_dict['date'])
        comment = thread.post(text=comment_dict['comment'], timestamp=ts)
        comment.author_id = self.get_user_id(comment_dict['submitter'])

    def make_attachment(self, org_ticket_id, ticket_id, att_dict):
        import urllib2
        if att_dict['size'] > self.ATTACHMENT_SIZE_LIMIT:
            self.errors.append('Ticket #%s: Attachment %s (@ %s) is too large, skipping' %
                               (org_ticket_id, att_dict['filename'], att_dict['url']))
            return
        f = urllib2.urlopen(att_dict['url'])
        TM.TicketAttachment.save_attachment(att_dict['filename'], ResettableStream(f),
                                            ticket_id=ticket_id)
        f.close()

    def collect_users(self, artifacts):
        users = set()
        for a in artifacts:
            users.add(a['submitter'])
            users.add(a['assigned_to'])
            for c in a['comments']:
                users.add(c['submitter'])
        return users
                
    def find_unknown_users(self, users):
        unknown  = set()
        for u in users:
            if u and not M.User.by_username(u):
                unknown.add(u)
        return unknown

    def make_user_placeholders(self, usernames):
        for username in usernames:
            M.User.register(dict(username=username,
                                 display_name=username), False)
        ThreadLocalORMSession.flush_all()
        log.info('Created %d user placeholders', len(usernames))


    def validate_import(self, doc):
        log.info('validate_migration called: %s', doc)
        migrator = ImportSupport()
        errors = []
        warnings = []

        artifacts = json.loads(doc)
        users = self.collect_users(artifacts)
        unknown_users = self.find_unknown_users(users)
        unknown_users = sorted(list(unknown_users))
        self.warnings.append('Document contains unknown users: %s' % unknown_users)
            
        return self.errors, self.warnings

    def perform_import(self, doc, **options):
        log.info('import called: %s', options) 
        self.init_options(options)
        artifacts = json.loads(doc)
        if self.option('create_users'):
            users = self.collect_users(artifacts)
            unknown_users = self.find_unknown_users(users)
            self.make_user_placeholders(unknown_users)
        
        M.session.artifact_orm_session._get().skip_mod_date = True
        for a in artifacts:
            comments = []
            attachments = []
            if 'comments' in a:
                comments = a['comments']
                del a['comments']
            if 'attachments' in a:
                attachments = a['attachments']
                del a['attachments']
#            log.info(a)
            t = self.make_artifact(a)
            for c_entry in comments:
                self.make_comment(t.discussion_thread, c_entry)
            for a_entry in attachments:
                self.make_attachment(a['id'], t._id, a_entry)
            log.info('Imported ticket: %d', t.ticket_num)

        return True
