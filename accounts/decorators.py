from functools import wraps
from django.shortcuts import render, redirect


def role_required(*allowed_roles):
    """
    Decorator for views that checks whether a user has a specific role.
    Usage:
        @role_required('ADMIN')
        @role_required('ADMIN', 'SALES')
        @role_required(['ADMIN', 'SALES'])
    """
    if len(allowed_roles) == 1 and isinstance(allowed_roles[0], (list, tuple)):
        roles = set(allowed_roles[0])
    else:
        roles = set(allowed_roles)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('login')

            user_role = getattr(request.user, 'role', None)
            if request.user.is_superuser or (user_role and user_role in roles):
                return view_func(request, *args, **kwargs)

            return render(request, '403.html', {
                'required_roles': sorted(list(roles)),
                'user_role': user_role or 'UNKNOWN',
            }, status=403)

        return _wrapped_view

    return decorator
