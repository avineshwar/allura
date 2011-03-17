import os

import tg
import pkg_resources
from pylons import c
from ming.orm import ThreadLocalORMSession

from allura import model as M
from allura.lib import helpers as h
from alluratest.controller import TestController


class TestRootController(TestController):

    def setUp(self):
        TestController.setUp(self)
        h.set_context('test', 'src-git')
        repo_dir = pkg_resources.resource_filename(
            'forgegit', 'tests/data')
        c.app.repo.fs_path = repo_dir
        c.app.repo.status = 'ready'
        c.app.repo.name = 'testgit.git'
        c.app.repo.refresh()
        ThreadLocalORMSession.flush_all()
        ThreadLocalORMSession.close_all()

    def test_index(self):
        resp = self.app.get('/src-git/').follow().follow()
        assert 'git://' in resp

    def test_index_empty(self):
        self.app.get('/git/')

    def test_log(self):
        resp = self.app.get('/src-git/ref/master:/log/')

    def _get_ci(self):
        r = self.app.get('/src-git/ref/master:/')
        resp = r.follow()
        for tag in resp.html.findAll('a'):
            if tag['href'].startswith('/p/test/src-git/ci/'):
                return tag['href']
        return None

    def test_commit(self):
        ci = self._get_ci()
        resp = self.app.get(ci)
        assert 'Rick' in resp, resp.showbrowser()

    def test_feed(self):
        assert 'Add README' in self.app.get('/feed')

    def test_tree(self):
        ci = self._get_ci()
        resp = self.app.get(ci + 'tree/')
        assert len(resp.html.findAll('tr')) == 2, resp.showbrowser()
        resp = self.app.get(ci + 'tree/')
        assert 'README' in resp, resp.showbrowser()
        links = [ a['href'] for a in resp.html.findAll('a') ]
        assert 'README' in links, resp.showbrowser()
        assert 'README/' not in links, resp.showbrowser()

    def test_tree_extra_params(self):
        ci = self._get_ci()
        resp = self.app.get(ci + 'tree/?format=raw')
        assert 'README' in resp, resp.showbrowser()

    def test_file(self):
        ci = self._get_ci()
        resp = self.app.get(ci + 'tree/README')
        assert 'README' in resp.html.find('h2',{'class':'dark title'}).contents[2]
        assert 'This is readme' in resp.html.find('div',{'class':'clip grid-19'}).contents[2]

    def test_invalid_file(self):
        ci = self._get_ci()
        self.app.get(ci + 'tree/READMEz', status=404)

    def test_diff(self):
        ci = self._get_ci()
        resp = self.app.get(ci + 'tree/README?diff=df30427c488aeab84b2352bdf88a3b19223f9d7a')
        assert 'readme' in resp, resp.showbrowser()
        assert '+++' in resp, resp.showbrowser()

    def test_refresh(self):
        notification = M.Notification.query.find(
            dict(subject='[test:src-git] 4 new commits to test Git')).first()
        domain = '.'.join(reversed(c.app.url[1:-1].split('/'))).replace('_', '-')
        common_suffix = tg.config.get('forgemail.domain', '.sourceforge.net')
        email = 'noreply@%s%s' % (domain, common_suffix)
        assert email in notification['reply_to_address']

    def test_file_force_display(self):
        ci = self._get_ci()
        resp = self.app.get(ci + 'tree/README?force=True')
        content = str(resp.html.find('div',{'class':'clip grid-19'}))
        assert '<pre>This is readme' in content, content
        assert '</pre>' in content, content
