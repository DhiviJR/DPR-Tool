"""
Django management command to create 5 admin users.
Usage: python manage.py create_admin_users
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from accounts.models import UserProfile

User = get_user_model()

ADMIN_USERS = [
    {
        'username': 'admin1',
        'password': 'adminuser1@MES',
        'email': 'admin1@mesinstruments.co.in',
        'first_name': 'Admin',
        'last_name': 'One',
    },
    {
        'username': 'admin2',
        'password': 'adminuser2@MES',
        'email': 'admin2@mesinstruments.co.in',
        'first_name': 'Admin',
        'last_name': 'Two',
    },
    {
        'username': 'admin3',
        'password': 'adminuser3@MES',
        'email': 'admin3@mesinstruments.co.in',
        'first_name': 'Admin',
        'last_name': 'Three',
    },
    {
        'username': 'admin4',
        'password': 'adminuser4@MES',
        'email': 'admin4@mesinstruments.co.in',
        'first_name': 'Admin',
        'last_name': 'Four',
    },
    {
        'username': 'admin5',
        'password': 'adminuser5@MES',
        'email': 'admin5@mesinstruments.co.in',
        'first_name': 'Admin',
        'last_name': 'Five',
    },
]


class Command(BaseCommand):
    help = 'Create 5 admin users with ADMIN role for DPR Tool'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE('Creating 5 Admin Users...'))
        self.stdout.write('-' * 50)

        created_count = 0
        skipped_count = 0

        for user_data in ADMIN_USERS:
            username = user_data['username']
            password = user_data['password']

            if User.objects.filter(username=username).exists():
                self.stdout.write(
                    self.style.WARNING(f'  [SKIP] User "{username}" already exists.')
                )
                skipped_count += 1
                continue

            # Create user
            user = User.objects.create_user(
                username=username,
                password=password,
                email=user_data['email'],
                first_name=user_data['first_name'],
                last_name=user_data['last_name'],
                user_type='admin',
                is_staff=True,       # Allow Django admin panel access
                is_superuser=False,  # Not superuser, but ADMIN role via profile
                is_active=True,
            )

            # Set UserProfile role to ADMIN
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = UserProfile.ROLE_ADMIN
            profile.save()

            self.stdout.write(
                self.style.SUCCESS(
                    f'  [OK] Created user: {username} | Role: ADMIN | Email: {user_data["email"]}'
                )
            )
            created_count += 1

        self.stdout.write('-' * 50)
        self.stdout.write(
            self.style.SUCCESS(
                f'Done! Created: {created_count} | Skipped (already exists): {skipped_count}'
            )
        )
        self.stdout.write('')
        self.stdout.write('Login credentials summary:')
        self.stdout.write('-' * 50)
        for user_data in ADMIN_USERS:
            self.stdout.write(
                f"  Username: {user_data['username']:<15} | Password: {user_data['password']}"
            )
        self.stdout.write('-' * 50)
