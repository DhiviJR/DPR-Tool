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
            'can_view_rfq': False,
            'can_view_customer_order': False,
            'can_view_dpr': False,
            'can_view_customer_po': False,
            'can_view_supplier_po': False,
            'can_view_masters': False,
        }

    role = getattr(request.user, 'role', 'ADMIN')
    is_admin = (role == 'ADMIN' or request.user.is_superuser)
    is_sales = (role == 'SALES')
    is_purchase = (role == 'PURCHASE')

    return {
        'user_role': role,
        'is_admin': is_admin,
        'is_sales': is_sales,
        'is_purchase': is_purchase,
        'can_view_rfq': is_admin or is_sales,
        'can_view_customer_order': is_admin or is_sales or is_purchase,
        'can_view_dpr': is_admin or is_purchase,
        'can_view_customer_po': is_admin or is_purchase,
        'can_view_supplier_po': is_admin or is_purchase,
        'can_view_masters': is_admin,
    }
