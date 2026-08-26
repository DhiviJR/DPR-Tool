from django.core.management.base import BaseCommand, CommandError

from email_classifier.models import EmailRecord
from email_classifier.services.classifier_service import get_classifier
from email_classifier.services.imap_reader import fetch_all_messages


class Command(BaseCommand):
    help = 'Read Inbox emails in small batches and classify them with Ollama. Does not modify the mailbox.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=20,
            help='Number of emails to process per run. Default: 20.',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')
        completed_before = EmailRecord.objects.filter(source='imap').count()
        self.stdout.write(
            f'Reading Inbox batch starting after {completed_before} saved email(s)...'
        )
        try:
            messages, inbox_total = fetch_all_messages(
                offset=completed_before, limit=batch_size
            )
        except Exception as exc:
            raise CommandError(f'Inbox read failed: {exc}') from exc

        self.stdout.write(
            f'Inbox contains {inbox_total} email(s). Classifying {len(messages)} in this batch...'
        )
        classifier = get_classifier()
        saved = 0
        skipped = 0
        for position, message in enumerate(messages, start=1):
            if EmailRecord.objects.filter(imap_uid=message['uid']).exists():
                skipped += 1
                continue
            try:
                safe_subj = message["subject"][:70].encode('ascii', errors='replace').decode('ascii')
                self.stdout.write(
                    f'Classifying {position}/{len(messages)}: {safe_subj}'
                )
                result = classifier.classify(
                    subject=message['subject'], body=message['body'], sender=message['sender'],
                )
                EmailRecord.objects.create(
                    sender=message['sender'], subject=message['subject'], body=message['body'],
                    source='imap', imap_uid=message['uid'],
                    received_at=message.get('date'),
                    ai_category=result['category'],
                    confidence=result['confidence'],
                    reason=result['reason'],
                    important_details=result['important_details'],
                )
                saved += 1
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f"Skipped email #{position}: {exc}"
                ))
        self.stdout.write(self.style.SUCCESS(
            f'Completed: {saved} new email(s) classified; {skipped} already saved. '
            f'Progress: {completed_before + saved}/{inbox_total}.'
        ))
