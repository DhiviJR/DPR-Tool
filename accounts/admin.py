from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Role Profile'
    fk_name = 'user'


class CustomUserAdmin(UserAdmin):
    inlines = (UserProfileInline,)
    list_display = ('username', 'email', 'first_name', 'last_name', 'get_role', 'is_staff')

    def get_role(self, obj):
        return obj.role
    get_role.short_description = 'Role'

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, UserProfile):
                existing_profile = UserProfile.objects.filter(user=instance.user).first()
                if existing_profile:
                    existing_profile.role = instance.role
                    existing_profile.save()
                    continue
            instance.save()
        formset.save_m2m()


admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(UserProfile)

