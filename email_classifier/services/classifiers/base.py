from abc import ABC, abstractmethod


class BaseEmailClassifier(ABC):
    @abstractmethod
    def classify(self, subject: str, body: str, sender: str = '') -> dict:
        """Return category, confidence, reason, and important_details."""
