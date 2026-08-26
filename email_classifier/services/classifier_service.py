from django.conf import settings


def get_classifier():
    if settings.EMAIL_CLASSIFIER_BACKEND == 'ollama':
        from .classifiers.ollama_classifier import OllamaEmailClassifier
        return OllamaEmailClassifier(settings.OLLAMA_MODEL, settings.OLLAMA_HOST)
    if settings.EMAIL_CLASSIFIER_BACKEND == 'rules':
        from .classifiers.rule_classifier import RuleBasedEmailClassifier
        return RuleBasedEmailClassifier()
    raise ValueError(f'Unsupported EMAIL_CLASSIFIER_BACKEND: {settings.EMAIL_CLASSIFIER_BACKEND}')
