def rbac_context(request):
    """
    Context processor to inject RBAC role flags and module visibility booleans into template context.
    """
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {
            'user_role': None,
            'is_admin': False,
            'is_sales': False,
            'is_purchase': False,
            'is_accounts': False,
            'can_view_rfq': False,
            'can_view_customer_order': False,
            'can_view_dpr': False,
            'can_view_customer_po': False,
            'can_view_supplier_po': False,
            'can_view_accounts': False,
            'can_view_masters': False,
            'can_view_customer_details': False,
        }

    role = getattr(request.user, 'role', 'ADMIN')
    is_admin = (role == 'ADMIN' or request.user.is_superuser)
    is_sales = (role == 'SALES')
    is_purchase = (role == 'PURCHASE')
    is_accounts = (role == 'ACCOUNTS')

    return {
        'user_role': role,
        'is_admin': is_admin,
        'is_sales': is_sales,
        'is_purchase': is_purchase,
        'is_accounts': is_accounts,
        'can_view_rfq': is_admin or is_sales,
        'can_view_customer_order': is_admin or is_purchase,
        'can_view_dpr': is_admin or is_purchase,
        'can_view_customer_po': is_admin or is_sales,
        'can_view_supplier_po': is_admin or is_purchase,
        'can_view_accounts': is_admin or is_accounts,
        'can_view_masters': is_admin,
        'can_view_customer_details': is_admin or is_sales,
    }
