import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import get_callable
from rest_framework import status
from rest_framework.exceptions import bad_request

logger = logging.getLogger('django-idempotency-key.idempotency_key.middleware')


def _get_storage_class():
    idkey_settings = getattr(settings, 'IDEMPOTENCY_KEY', dict())
    return get_callable(idkey_settings.get('STORAGE_CLASS', 'idempotency_key.storage.MemoryKeyStorage'))


def _get_encoder_class():
    idkey_settings = getattr(settings, 'IDEMPOTENCY_KEY', dict())
    return get_callable(idkey_settings.get('ENCODER_CLASS', 'idempotency_key.encoders.BasicKeyEncoder'))


def _get_conflict_code():
    idkey_settings = getattr(settings, 'IDEMPOTENCY_KEY', dict())
    return idkey_settings.get('CONFLICT_STATUS_CODE', status.HTTP_409_CONFLICT)


class IdempotencyKeyMiddleware:
    """
    This middleware class assumes that all non-safe HTTP methods will require an idempotency key to be specified in
    the header.
    View functions can opt-out using the @idempotency_key_exempt decorator
    """

    def __init__(self, get_response=None):
        self.get_response = get_response
        self.storage = _get_storage_class()()
        self.encoder = _get_encoder_class()()

    def __call__(self, request):
        self.process_request(request)
        response = self.get_response(request)
        response = self.process_response(request, response)
        return response

    @staticmethod
    def _reject(request, reason):
        response = bad_request(request, None)
        logger.error(
            'Error (%s): %s', reason, request.path,
            extra={
                'status_code': 400,
                'request': request,
            }
        )
        return response

    def _set_flags_from_callback(self, request, callback):
        # If there is an actions attribute then the function is wrapped in a DRF viewset
        if hasattr(callback, 'actions'):
            # get a reference to the function to access any attributes we might be interested in.
            callback = getattr(callback.cls, callback.actions[request.method.lower()], callback)

        request.idempotency_key_exempt = getattr(callback, 'idempotency_key_exempt', False)
        request.idempotency_key_manual = getattr(callback, 'idempotency_key_manual', False)

    def process_request(self, request):
        key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if key is not None:
            request.META['IDEMPOTENCY_KEY'] = key

        # Use this attribute to check that process_view has been called.
        request.idempotency_key_done = False

    def process_view(self, request, callback, callback_args, callback_kwargs):
        self._set_flags_from_callback(request, callback)

        # signal the process_view has been called
        request.idempotency_key_done = True

        # Assume that anything defined as 'safe' by RFC7231 is exempt or if exempt is specified directly
        if request.idempotency_key_exempt or request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            request.idempotency_key_exempt = True
            return None

        # At this point the view function is not exempt so mark it as such
        request.idempotency_key_exempt = False

        key = request.META.get('IDEMPOTENCY_KEY')
        if key is None:
            return self._reject(request, 'Idempotency key is required and was not specified in the header.')

        # Has the manual override decorator been specified? if so add it to the request
        if request.idempotency_key_manual:
            request.use_idempotency_key_manual_override = True

        # encode the key and add it to the request
        encoded_key = request.idempotency_key_encoded_key = self.encoder.encode_key(request, key)

        # Check if a response already exists for the encoded key
        key_exists, response = self.storage.retrieve_data(encoded_key)

        # add the key exists result and the original request if it exists
        request.idempotency_key_exists = key_exists
        request.idempotency_key_response = response

        # If not manual override and the key already exists then return the original response as a 409 CONFLICT
        if not request.idempotency_key_manual and key_exists:
            status_code = _get_conflict_code()
            if status_code is not None:
                response.status_code = status_code
            return response

        return None

    def process_response(self, request, response):
        # Make sure that process_view is called otherwise the use of idempotency keys will be overridden without us
        # knowing about it.
        if not getattr(request, 'idempotency_key_done', False):
            raise ImproperlyConfigured(
                'Idempotency key middleware\'s \'process_view\' function was not called! '
                'There maybe another middleware stopping this from happening which means your functions will not '
                'be properly protected with idempotency keys.'
            )

        if getattr(request, 'idempotency_key_exempt', True):
            return response

        if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            # If the response is 2XX then store the response
            if status.HTTP_200_OK <= response.status_code <= status.HTTP_207_MULTI_STATUS:
                self.storage.store_data(request.idempotency_key_encoded_key, response)

        return response


class ExemptIdempotencyKeyMiddleware(IdempotencyKeyMiddleware):
    """
    This middleware class assume all requests are exempt and do not require an idempotency key to be specified.
    View functions opt-in using the @idempotency_key or @idempotency_key_manual decorators.
    """

    def _set_flags_from_callback(self, request, callback):
        # If there is an actions attribute then the function is wrapped in a DRF viewset
        if hasattr(callback, 'actions'):
            # get a reference to the function to access any attributes we might be interested in.
            callback = getattr(callback.cls, callback.actions[request.method.lower()], callback)

        idempotency_key = getattr(callback, 'idempotency_key', None)
        idempotency_key_exempt = getattr(callback, 'idempotency_key_exempt', None)
        idempotency_key_manual = getattr(callback, 'idempotency_key_manual', False)

        request.idempotency_key_exempt = idempotency_key_exempt or (
                idempotency_key_exempt is None and not idempotency_key_manual and not idempotency_key
        )

        request.idempotency_key_manual = idempotency_key_manual
