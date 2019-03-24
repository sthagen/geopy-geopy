"""
Adapters are HTTP client implementations used by geocoders.

Some adapters might support keep-alives, request retries, different HTTP
protocols, persistence of Cookies, compression and so on.

Adapters should be considered an implementation detail. Most of the time
you wouldn't need to know about their existence.

.. versionadded:: 2.0
   Adapters are currently provided on a `provisional basis`_.

    .. _provisional basis: https://docs.python.org/3/glossary.html#term-provisional-api
"""
import abc
import json
from socket import timeout as SocketTimeout
from ssl import SSLError
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, URLError, build_opener

from geopy.exc import (
    GeocoderParseError,
    GeocoderServiceError,
    GeocoderTimedOut,
    GeocoderUnavailable,
)
from geopy.util import logger


class AdapterHTTPError(Exception):
    """An exception which should be raised by adapters when an HTTP response
    with a non-successful status code has been received.
    """

    def __init__(self, *args, **kwargs):
        self.status_code = kwargs.pop("status_code")
        self.text = kwargs.pop("text")
        super().__init__(*args, **kwargs)


class BaseAdapter(abc.ABC):
    """Base class for an Adapter.

    To create a custom adapter implementation, add an implementation
    of this class and specify it in
    the :attr:`geopy.geocoders.options.adapter_factory` value.

    The :attr:`geopy.geocoders.options.adapter_factory` value is a callable
    which accepts two keyword args: ``proxies`` and ``ssl_context``.
    If that is correct for your case, just assign your new class to
    that attribute. Otherwise you might need to do something like this::

        geopy.geocoders.options.adapter_factory = (
            lambda proxies, ssl_context: MyAdapter(
                proxies=proxies, ssl_context=ssl_context, my_custom_arg=42
            )
        )

    """

    # A class attribute which tells if this Adapter's dependencies
    # are installed. By default assume that all Adapters are available.
    is_available = True

    def __init__(self, *, proxies, ssl_context):
        """Initialize adapter.

        :param dict proxies: An urllib-style proxies dict, e.g.
            ``{"http": "192.0.2.0:8080", "https": "192.0.2.0:8080"}``.
            See :attr:`geopy.geocoders.options.default_proxies` (note
            that the Adapters always receive a dict: the string proxy
            is transformed to a dict in the base
            :class:`geopy.geocoders.base.Geocoder` class.).

        :type ssl_context: :class:`ssl.SSLContext`
        :param ssl_context:
            See :attr:`geopy.geocoders.options.default_ssl_context`.

        """
        pass

    @abc.abstractmethod
    def get_json(self, url, *, timeout, headers):
        """Same as ``get_text`` except that the response is expected
        to be a valid JSON. The value returned is the parsed JSON.

        :class:`geopy.exc.GeocoderParseError` must be raised if
        the response cannot be parsed.

        :param str url: The target URL.

        :param float timeout:
            See :attr:`geopy.geocoders.options.default_timeout`.

        :param dict headers: A dict with custom HTTP request headers.
        """
        pass

    @abc.abstractmethod
    def get_text(self, url, *, timeout, headers):
        """Make a GET request and return the response as string.

        This method should not raise any exceptions other than these:

        - :class:`geopy.exc.AdapterHTTPError` should be raised if the response
          was successfully retrieved but the status code was non-successful.
        - :class:`geopy.exc.GeocoderTimedOut` should be raised when the request
          times out.
        - :class:`geopy.exc.GeocoderUnavailable` should be raised when the target
          host is unreachable.
        - :class:`geopy.exc.GeocoderServiceError` is the least specific error
          in the exceptions hierarchy and should be raised in any other cases.

        :param str url: The target URL.

        :param float timeout:
            See :attr:`geopy.geocoders.options.default_timeout`.

        :param dict headers: A dict with custom HTTP request headers.
        """
        pass


class URLLibAdapter(BaseAdapter):
    """The fallback adapter which uses urllib from the Python standard
    library, see :func:`urllib.request.urlopen`.

    urllib doesn't support keep-alives, doesn't persist Cookies
    and is HTTP/1.1 only.

    """

    def __init__(self, *, proxies, ssl_context):
        super().__init__(proxies=proxies, ssl_context=ssl_context)

        # `ProxyHandler` should be present even when actually there're
        # no proxies. `build_opener` contains it anyway. By specifying
        # it here explicitly we can disable system proxies (i.e.
        # from HTTP_PROXY env var) by setting `proxies` to `{}`.
        # Otherwise, if we didn't specify ProxyHandler for empty
        # `proxies` here, the `build_opener` would have used one internally
        # which could have unwillingly picked up the system proxies.
        opener = build_opener(
            HTTPSHandler(context=ssl_context),
            ProxyHandler(proxies),
        )
        self.urlopen = opener.open

    def get_json(self, url, *, timeout, headers):
        text = self.get_text(url, timeout=timeout, headers=headers)
        try:
            return json.loads(text)
        except ValueError:
            raise GeocoderParseError(
                "Could not deserialize using deserializer:\n%s" % text
            )

    def get_text(self, url, *, timeout, headers):
        req = Request(url=url, headers=headers)
        try:
            page = self.urlopen(req, timeout=timeout)
        except Exception as error:
            message = str(error.args[0]) if len(error.args) else str(error)
            if isinstance(error, HTTPError):
                code = error.getcode()
                body = self._read_http_error_body(error)
                raise AdapterHTTPError(message, status_code=code, text=body)
            elif isinstance(error, URLError):
                if "timed out" in message:
                    raise GeocoderTimedOut("Service timed out")
                elif "unreachable" in message:
                    raise GeocoderUnavailable("Service not available")
            elif isinstance(error, SocketTimeout):
                raise GeocoderTimedOut("Service timed out")
            elif isinstance(error, SSLError):
                if "timed out" in message:
                    raise GeocoderTimedOut("Service timed out")
            raise GeocoderServiceError(message)
        else:
            text = self._decode_page(page)
            status_code = page.getcode()
            if status_code >= 400:
                raise AdapterHTTPError(
                    "Non-successful status code", status_code=status_code, text=text
                )

        return text

    def _read_http_error_body(self, error):
        try:
            return self._decode_page(error)
        except Exception:
            logger.debug(
                "Unable to fetch body for a non-successful HTTP response", exc_info=True
            )
            return None

    def _decode_page(self, page):
        encoding = page.headers.get_content_charset() or "utf-8"
        try:
            body_bytes = page.read()
        except Exception:
            raise GeocoderServiceError("Unable to read the response")

        try:
            return str(body_bytes, encoding=encoding)
        except ValueError:
            raise GeocoderParseError("Unable to decode the response bytes")
