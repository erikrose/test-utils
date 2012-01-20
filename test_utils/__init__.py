from django import test
from django.conf import settings
from django.core import cache, management, mail
from django.core.management import call_command
from django.db import connection, connections, DEFAULT_DB_ALIAS, transaction
from django.db.models import loading
from django.test.client import RequestFactory as DjangoRequestFactory
from django.utils.encoding import smart_unicode as unicode
from django.utils.translation import trans_real
from django.utils.translation.trans_real import to_language

from django_nose import uses_mysql, FastFixtureTestCase
from nose.tools import eq_
from nose import SkipTest

from test_utils import signals


VERSION = (0, 3)
__version__ = '.'.join(map(str, VERSION))

# We only want to run through setup_test_environment once.
IS_SETUP = False
TEST_UTILS_NO_TRUNCATE = set(getattr(settings, 'TEST_UTILS_NO_TRUNCATE', ()))


def setup_test_environment():
    """Our own setup that hijacks Jinja template rendering."""
    global IS_SETUP
    if IS_SETUP:
        return
    IS_SETUP = True

    # Import here so it's not required to install test-utils.
    import jinja2
    old_render = jinja2.Template.render

    def instrumented_render(self, *args, **kwargs):
        context = dict(*args, **kwargs)
        test.signals.template_rendered.send(sender=self, template=self,
                                            context=context)
        return old_render(self, *args, **kwargs)

    jinja2.Template.render = instrumented_render

    try:
        from celery.app import current_app
        current_app().conf.CELERY_ALWAYS_EAGER = True
    except ImportError:
        pass

    try:
        import async_signals
        async_signals.stop_the_machine()
        settings.ASYNC_SIGNALS = False
    except ImportError:
        pass


class BaseTestCase(object):

    def __init__(self, *args, **kwargs):
        setup_test_environment()
        super(BaseTestCase, self).__init__(*args, **kwargs)

    def _pre_setup(self):
        # allow others to prepare
        signals.pre_setup.send(sender=self.__class__)
        cache.cache.clear()
        settings.CACHE_COUNT_TIMEOUT = None
        settings.TEMPLATE_DEBUG = settings.DEBUG = False
        super(BaseTestCase, self)._pre_setup()

    def _post_teardown(self):
        super(BaseTestCase, self)._post_teardown()
        # allow others to clean up
        signals.post_teardown.send(sender=self.__class__)


class TransactionTestCase(BaseTestCase, test.TransactionTestCase):
    """
    Subclass of ``django.test.TransactionTestCase`` that quickly tears down
    fixtures and doesn't `flush` on setup.  This enables tests to be run in
    any order.
    """

    def _fixture_setup(self):
        """We omit the flush since it's slow and not needed since we properly
        tear down our fixtures."""
        # If the test case has a multi_db=True flag, flush all databases.
        # Otherwise, just flush default.
        if getattr(self, 'multi_db', False):
            databases = connections
        else:
            databases = [DEFAULT_DB_ALIAS]
        for db in databases:
            if hasattr(self, 'fixtures'):
                # We have to use this slightly awkward syntax due to the fact
                # that we're using *args and **kwargs together.
                management.call_command('loaddata', *self.fixtures,
                                        **{'verbosity': 0, 'database': db})

    def _fixture_teardown(self):
        """Executes a quick truncation of MySQL tables."""
        cursor = connection.cursor()
        using_mysql = uses_mysql(connection)
        if using_mysql:
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')
        table = connection.introspection.django_table_names()
        for table in set(table) - TEST_UTILS_NO_TRUNCATE:
            if using_mysql:
                cursor.execute('TRUNCATE `%s`' % table)
            else:
                cursor.execute('DELETE FROM %s' % table)

        cursor.close()


class TestCase(FastFixtureTestCase):
    """``TestCase`` subclass providing fast fixtures and Mozilla specifics

    Provides:
        * Jinja template hijacking
        * Signals for hooking setup and teardown
        * A cache-machine timeout
        * On-thread celery execution
        * Deactivation of any l10n locales

    """
    def __init__(self, *args, **kwargs):
        setup_test_environment()
        super(TestCase, self).__init__(*args, **kwargs)

    def _pre_setup(self):
        """Adjust cache-machine settings, and send custom pre-setup signal."""
        signals.pre_setup.send(sender=self.__class__)
        settings.CACHE_COUNT_TIMEOUT = None
        trans_real.deactivate()
        trans_real._translations = {}  # Django fails to clear this cache.
        trans_real.activate(settings.LANGUAGE_CODE)
        super(TestCase, self)._pre_setup()

    def _post_teardown(self):
        """Send custom post-teardown signal."""
        super(TestCase, self)._post_teardown()
        signals.post_teardown.send(sender=self.__class__)


class ExtraAppTestCase(FastFixtureTestCase):
    """
    ``TestCase`` subclass that lets you add extra apps just for testing.

    Configure extra apps through the class attribute ``extra_apps``, which is a
    sequence of 'app.module' strings. ::

        class FunTest(ExtraAppTestCase):
            extra_apps = ['fun.tests.testapp']
            ...
    """
    extra_apps = []

    @classmethod
    def setUpClass(cls):
        for app in cls.extra_apps:
            settings.INSTALLED_APPS += (app,)
            loading.load_app(app)
        management.call_command('syncdb', verbosity=0, interactive=False)
        super(ExtraAppTestCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        # Remove the apps from extra_apps.
        for app_label in cls.extra_apps:
            app_name = app_label.split('.')[-1]
            app = loading.cache.get_app(app_name)
            del loading.cache.app_models[app_name]
            del loading.cache.app_store[app]

        apps = set(settings.INSTALLED_APPS).difference(cls.extra_apps)
        settings.INSTALLED_APPS = tuple(apps)
        super(ExtraAppTestCase, cls).tearDownClass()


try:
    # You don't need a SeleniumTestCase if you don't have selenium.
    from selenium import selenium

    class SeleniumTestCase(TestCase):
        selenium = True

        def setUp(self):
            super(SeleniumTestCase, self).setUp()

            if not settings.SELENIUM_CONFIG:
                raise SkipTest()

            self.selenium = selenium(settings.SELENIUM_CONFIG['HOST'],
                                     settings.SELENIUM_CONFIG['PORT'],
                                     settings.SELENIUM_CONFIG['BROWSER'],
                                     settings.SITE_URL)
            self.selenium.start()

        def tearDown(self):
            self.selenium.close()
            self.selenium.stop()
            super(SeleniumTestCase, self).tearDown()
except ImportError:
    pass


class RequestFactory(DjangoRequestFactory):
    """
    Class that lets you create mock Request objects for use in testing.

    Usage::

        rf = RequestFactory()
        get_request = rf.get('/hello/')
        post_request = rf.post('/submit/', {'foo': 'bar'})

    Once you have a request object you can pass it to any view function, just
    as if that view had been hooked up using a URLconf.
    """
    def _base_environ(self, **request):
        # Add wsgi.input to base environ.
        # TODO: upstream to django and delete this.
        environ = super(RequestFactory, self)._base_environ(**request)
        if 'wsgi.input' not in environ:
            environ['wsgi.input'] = None
        return environ


# Comparisons

def locale_eq(a, b):
    """Compare two locales."""
    eq_(*map(to_language, [a, b]))


def trans_eq(translation, string, locale=None):
    eq_(unicode(translation), string)
    if locale:
        locale_eq(translation.locale, locale)
