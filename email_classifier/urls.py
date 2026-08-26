from django.urls import path
from . import views

app_name = 'email_classifier'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('sync-inbox/', views.sync_inbox, name='sync_inbox'),
    path('classify/', views.classify_email, name='classify_email'),
    path('emails/<int:record_id>/review/', views.review_email, name='review_email'),
    path('emails/<int:record_id>/toggle-rfq/', views.toggle_enquiry_status, name='toggle_enquiry_status'),
    path('emails/<int:record_id>/add-rfq/', views.add_rfq_from_email, name='add_rfq_from_email'),
    path('emails/<int:record_id>/add-po/', views.add_po_from_email, name='add_po_from_email'),
]
