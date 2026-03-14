from .base import ProviderProfile
from . import movies111, vidfast, vidlink, vidnest


PROVIDER_MODULES = {
    '111movies': movies111,
    'vidfast': vidfast,
    'vidlink': vidlink,
    'vidnest': vidnest,
}


DEFAULT_PROFILE = ProviderProfile(name='generic')


def get_provider_module(provider):
    return PROVIDER_MODULES.get((provider or '').lower())


def get_provider_profile(provider):
    module = get_provider_module(provider)
    if module and hasattr(module, 'PROFILE'):
        return module.PROFILE
    return DEFAULT_PROFILE
