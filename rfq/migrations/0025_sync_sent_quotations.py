from django.db import migrations


def sync_sent_quotations(apps, schema_editor):
    RFQ = apps.get_model('rfq', 'RFQ')
    RFQProduct = apps.get_model('rfq', 'RFQProduct')
    RFQQuotation = apps.get_model('rfq', 'RFQQuotation')
    RFQEmailMessage = apps.get_model('rfq', 'RFQEmailMessage')

    for rfq in RFQ.objects.all():
        out_messages = list(rfq.email_messages.filter(direction='OUT'))
        quotes = list(rfq.quotations.all())
        has_sent_quote = any(q.email_sent for q in quotes)

        # If there are outgoing messages and quotations exist
        if out_messages and quotes:
            for q in quotes:
                clean_no = (q.quotation_number or '').replace('/', '_')
                matched = len(quotes) == 1
                if not matched:
                    for msg in out_messages:
                        att_str = msg.attachment_names or ''
                        sub_str = msg.subject or ''
                        if (clean_no and clean_no in att_str) or (q.quotation_number and q.quotation_number in sub_str):
                            matched = True
                            break
                if matched and not q.email_sent:
                    q.email_sent = True
                    q.save(update_fields=['email_sent'])
                    has_sent_quote = True

        # For any quotation marked email_sent, sync the products and RFQ
        for q in quotes:
            if q.email_sent:
                has_sent_quote = True
                snapshot = q.products_snapshot or []
                product_ids = [item.get('product_id') for item in snapshot if isinstance(item, dict) and item.get('product_id')]
                if product_ids:
                    RFQProduct.objects.filter(rfq=rfq, id__in=product_ids).update(
                        quotation_email_sent=True,
                        quotation_prepared=True
                    )
                else:
                    RFQProduct.objects.filter(rfq=rfq, price_known=True, value__gt=0).update(
                        quotation_email_sent=True,
                        quotation_prepared=True
                    )

        if has_sent_quote or (out_messages and quotes):
            update_fields = []
            if not rfq.quotation_email_sent:
                rfq.quotation_email_sent = True
                update_fields.append('quotation_email_sent')
            if not rfq.quotation_prepared:
                rfq.quotation_prepared = True
                update_fields.append('quotation_prepared')
            if not rfq.email_sent_date and out_messages:
                rfq.email_sent_date = out_messages[0].sent_at
                update_fields.append('email_sent_date')
            if update_fields:
                rfq.save(update_fields=update_fields)


def reverse_sync(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0024_rfqattachment'),
    ]

    operations = [
        migrations.RunPython(sync_sent_quotations, reverse_sync),
    ]
