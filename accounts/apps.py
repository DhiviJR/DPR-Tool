from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        from django.core.signals import request_started
        from .email_config_utils import load_email_settings_to_runtime

        _loaded = {'done': False}

        def _on_request(sender, **kwargs):
            if not _loaded['done']:
                try:
                    load_email_settings_to_runtime()
                    _loaded['done'] = True
                except Exception:
                    pass

        request_started.connect(_on_request, dispatch_uid="accounts_lazy_load_email_settings")
