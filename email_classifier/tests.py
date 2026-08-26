import datetime
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .models import EmailRecord

User = get_user_model()


class EmailDashboardDateFilterTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password123')
        self.client.login(username='testuser', password='password123')

        now = timezone.now()
        local_now = timezone.localtime(now)
        today_start = local_now.replace(hour=10, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - datetime.timedelta(days=1)
        last_month_start = today_start - datetime.timedelta(days=40)

        # Email today - Customer Order
        EmailRecord.objects.create(
            sender='customer1@example.com',
            subject='PO Today',
            body='Order body',
            ai_category='CUSTOMER_ORDER',
            received_at=today_start
        )

        # Email yesterday - Quotation Request
        EmailRecord.objects.create(
            sender='customer2@example.com',
            subject='Quote Yesterday',
            body='RFQ body',
            ai_category='QUOTATION_REQUEST',
            received_at=yesterday_start
        )

        # Email last month - Payment Invoice
        EmailRecord.objects.create(
            sender='customer3@example.com',
            subject='Invoice Old',
            body='Invoice body',
            ai_category='PAYMENT_INVOICE',
            received_at=last_month_start
        )

    def test_dashboard_all_time_filter(self):
        response = self.client.get(reverse('email_classifier:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_emails_count'], 3)
        self.assertEqual(response.context['counts']['CUSTOMER_ORDER'], 1)
        self.assertEqual(response.context['counts']['QUOTATION_REQUEST'], 1)
        self.assertEqual(response.context['counts']['PAYMENT_INVOICE'], 1)

    def test_dashboard_today_filter(self):
        response = self.client.get(reverse('email_classifier:dashboard'), {'date_filter': 'today'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_emails_count'], 1)
        self.assertEqual(response.context['counts']['CUSTOMER_ORDER'], 1)
        self.assertEqual(response.context['counts']['QUOTATION_REQUEST'], 0)

    def test_dashboard_yesterday_filter(self):
        response = self.client.get(reverse('email_classifier:dashboard'), {'date_filter': 'yesterday'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_emails_count'], 1)
        self.assertEqual(response.context['counts']['QUOTATION_REQUEST'], 1)
        self.assertEqual(response.context['counts']['CUSTOMER_ORDER'], 0)
