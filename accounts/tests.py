from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import CustomUser, UserProfile


class RBACTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Admin user
        self.admin_user = CustomUser.objects.create_user(
            username='admin_test',
            password='Password123!',
        )
        self.admin_user.profile.role = UserProfile.ROLE_ADMIN
        self.admin_user.profile.save()

        # Sales user
        self.sales_user = CustomUser.objects.create_user(
            username='sales_test',
            password='Password123!',
        )
        self.sales_user.profile.role = UserProfile.ROLE_SALES
        self.sales_user.profile.save()

        # Purchase user
        self.purchase_user = CustomUser.objects.create_user(
            username='purchase_test',
            password='Password123!',
        )
        self.purchase_user.profile.role = UserProfile.ROLE_PURCHASE
        self.purchase_user.profile.save()

    def test_user_role_properties(self):
        self.assertEqual(self.admin_user.role, 'ADMIN')
        self.assertTrue(self.admin_user.is_admin)
        self.assertFalse(self.admin_user.is_sales)

        self.assertEqual(self.sales_user.role, 'SALES')
        self.assertTrue(self.sales_user.is_sales)
        self.assertFalse(self.sales_user.is_admin)

        self.assertEqual(self.purchase_user.role, 'PURCHASE')
        self.assertTrue(self.purchase_user.is_purchase)
        self.assertFalse(self.purchase_user.is_sales)

    def test_unauthenticated_redirect(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_admin_access_all_views(self):
        self.client.force_login(self.admin_user)
        for url_name in ['dashboard', 'dpr_view', 'customer_po_product_details', 'supplier_po_product_details', 'material_status', 'accounts_details', 'rfq_details', 'customer_details', 'supplier_details', 'customer_order', 'register']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, f"Admin failed to access {url_name}")

    def test_sales_access_permissions(self):
        self.client.force_login(self.sales_user)
        # Allowed views
        for url_name in ['dashboard', 'rfq_details', 'customer_po_product_details']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, f"Sales failed to access allowed view {url_name}")

        # Restricted views (403 Forbidden)
        for url_name in ['dpr_view', 'customer_order', 'supplier_po_product_details', 'material_status', 'accounts_details', 'customer_details', 'supplier_details', 'register']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 403, f"Sales was not blocked from accessing {url_name}")

    def test_purchase_access_permissions(self):
        self.client.force_login(self.purchase_user)
        # Allowed views
        for url_name in ['dashboard', 'customer_order', 'dpr_view', 'customer_po_product_details', 'supplier_po_product_details', 'material_status']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, f"Purchase failed to access allowed view {url_name}")

        # Restricted views (403 Forbidden)
        for url_name in ['accounts_details', 'rfq_details', 'customer_details', 'supplier_details', 'register']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 403, f"Purchase was not blocked from accessing {url_name}")

    def test_create_user_profile_no_duplicate_error(self):
        new_user = CustomUser.objects.create_user(username='new_unique_user', password='password123')
        self.assertTrue(hasattr(new_user, 'profile'))
        # Attempt duplicate save to simulate admin inline formset save
        dup_profile = UserProfile(user=new_user, role=UserProfile.ROLE_SALES)
        dup_profile.save()
        self.assertEqual(new_user.profile.role, UserProfile.ROLE_SALES)
        self.assertEqual(UserProfile.objects.filter(user=new_user).count(), 1)

