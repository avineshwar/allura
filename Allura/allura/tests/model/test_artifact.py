# -*- coding: utf-8 -*-

#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

"""
Model tests for artifact
"""
import re
from datetime import datetime

from pylons import tmpl_context as c
from nose.tools import assert_raises, assert_equal
from nose import with_setup
from mock import patch
from ming.orm.ormsession import ThreadLocalORMSession
from ming.orm import Mapper
from bson import ObjectId
from webob import Request

import allura
from allura import model as M
from allura.lib import helpers as h
from allura.lib import security
from allura.tests import decorators as td
from allura.websetup.schema import REGISTRY
from alluratest.controller import setup_basic_test, setup_unit_test
from forgewiki import model as WM


class Checkmessage(M.Message):

    class __mongometa__:
        name = 'checkmessage'

    def url(self):
        return ''

    def __init__(self, **kw):
        super(Checkmessage, self).__init__(**kw)
        if self.slug is not None and self.full_slug is None:
            self.full_slug = datetime.utcnow().strftime(
                '%Y%m%d%H%M%S') + ':' + self.slug
Mapper.compile_all()


def setUp():
    setup_basic_test()
    setup_unit_test()
    setup_with_tools()


@td.with_wiki
def setup_with_tools():
    h.set_context('test', 'wiki', neighborhood='Projects')
    Checkmessage.query.remove({})
    WM.Page.query.remove({})
    WM.PageHistory.query.remove({})
    M.Shortlink.query.remove({})
    c.user = M.User.query.get(username='test-admin')
    Checkmessage.project = c.project
    Checkmessage.app_config = c.app.config


def tearDown():
    ThreadLocalORMSession.close_all()


@with_setup(setUp, tearDown)
def test_artifact():
    pg = WM.Page(title='TestPage1')
    assert pg.project == c.project
    assert pg.project_id == c.project._id
    assert pg.app.config == c.app.config
    assert pg.app_config == c.app.config
    u = M.User.query.get(username='test-user')
    pr = M.ProjectRole.by_user(u, upsert=True)
    ThreadLocalORMSession.flush_all()
    REGISTRY.register(allura.credentials, allura.lib.security.Credentials())
    assert not security.has_access(pg, 'delete')(user=u)
    pg.acl.append(M.ACE.allow(pr._id, 'delete'))
    ThreadLocalORMSession.flush_all()
    c.memoize_cache = {}
    assert security.has_access(pg, 'delete')(user=u)
    pg.acl.pop()
    ThreadLocalORMSession.flush_all()
    c.memoize_cache = {}
    assert not security.has_access(pg, 'delete')(user=u)


def test_artifact_index():
    pg = WM.Page(title='TestPage1')
    idx = pg.index()
    assert 'title' in idx
    assert 'url_s' in idx
    assert 'project_id_s' in idx
    assert 'mount_point_s' in idx
    assert 'type_s' in idx
    assert 'id' in idx
    assert idx['id'] == pg.index_id()
    assert 'text' in idx
    assert 'TestPage' in pg.shorthand_id()
    assert pg.link_text() == pg.shorthand_id()


@with_setup(setUp, tearDown)
def test_artifactlink():
    pg = WM.Page(title='TestPage2')
    q = M.Shortlink.query.find(dict(
        project_id=c.project._id,
        app_config_id=c.app.config._id,
        link=pg.shorthand_id()))
    assert q.count() == 0
    ThreadLocalORMSession.flush_all()
    M.MonQTask.run_ready()
    ThreadLocalORMSession.flush_all()
    assert q.count() == 1
    assert M.Shortlink.lookup('[TestPage2]')
    assert M.Shortlink.lookup('[wiki:TestPage2]')
    assert M.Shortlink.lookup('[test:wiki:TestPage2]')
    assert not M.Shortlink.lookup('[test:wiki:TestPage2:foo]')
    assert not M.Shortlink.lookup('[Wiki:TestPage2]')
    assert not M.Shortlink.lookup('[TestPage2_no_such_page]')
    c.project.uninstall_app('wiki')
    ThreadLocalORMSession.flush_all()
    assert not M.Shortlink.lookup('[wiki:TestPage2]')
    pg.delete()
    ThreadLocalORMSession.flush_all()
    M.MonQTask.run_ready()
    ThreadLocalORMSession.flush_all()
    assert q.count() == 0


@with_setup(setUp, tearDown)
def test_gen_messageid():
    assert re.match(r'[0-9a-zA-Z]*.wiki@test.p.localhost',
                    h.gen_message_id())


@with_setup(setUp, tearDown)
def test_gen_messageid_with_id_set():
    oid = ObjectId()
    assert re.match(r'%s.wiki@test.p.localhost' %
                    str(oid), h.gen_message_id(oid))


@with_setup(setUp, tearDown)
def test_artifact_messageid():
    p = WM.Page(title='T')
    assert re.match(r'%s.wiki@test.p.localhost' %
                    str(p._id), p.message_id())


@with_setup(setUp, tearDown)
def test_versioning():
    pg = WM.Page(title='TestPage3')
    with patch('allura.model.artifact.request',
               Request.blank('/', remote_addr='1.1.1.1')):
        pg.commit()
    ThreadLocalORMSession.flush_all()
    pg.text = 'Here is some text'
    pg.commit()
    ThreadLocalORMSession.flush_all()
    ss = pg.get_version(1)
    assert ss.author.logged_ip == '1.1.1.1'
    assert ss.index()['is_history_b']
    assert ss.shorthand_id() == pg.shorthand_id() + '#1'
    assert ss.title == pg.title
    assert ss.text != pg.text
    ss = pg.get_version(-1)
    assert ss.index()['is_history_b']
    assert ss.shorthand_id() == pg.shorthand_id() + '#2'
    assert ss.title == pg.title
    assert ss.text == pg.text
    assert_raises(IndexError, pg.get_version, 42)
    pg.revert(1)
    pg.commit()
    ThreadLocalORMSession.flush_all()
    assert ss.text != pg.text
    assert pg.history().count() == 3


@with_setup(setUp, tearDown)
def test_messages_unknown_lookup():
    from bson import ObjectId
    m = Checkmessage()
    m.author_id = ObjectId()  # something new
    assert type(m.author()) == M.User, type(m.author())
    assert m.author() == M.User.anonymous()


@with_setup(setUp, tearDown)
@patch('allura.model.artifact.datetime')
def test_last_updated(_datetime):
    c.project.last_updated = datetime(2014, 1, 1)
    _datetime.utcnow.return_value = datetime(2014, 1, 2)
    WM.Page(title='TestPage1')
    ThreadLocalORMSession.flush_all()
    assert_equal(c.project.last_updated, datetime(2014, 1, 2))


@with_setup(setUp, tearDown)
@patch('allura.model.artifact.datetime')
def test_last_updated_disabled(_datetime):
    c.project.last_updated = datetime(2014, 1, 1)
    _datetime.utcnow.return_value = datetime(2014, 1, 2)
    try:
        M.artifact_orm_session._get().skip_last_updated = True
        WM.Page(title='TestPage1')
        ThreadLocalORMSession.flush_all()
        assert_equal(c.project.last_updated, datetime(2014, 1, 1))
    finally:
        M.artifact_orm_session._get().skip_last_updated = False
