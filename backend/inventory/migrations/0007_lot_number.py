from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0006_barcodes_and_lots"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="lot",
            name="unique_lot_per_product",
        ),
        migrations.RenameField(
            model_name="lot",
            old_name="code",
            new_name="lot_number",
        ),
        migrations.RemoveField(
            model_name="lot",
            name="batch",
        ),
        migrations.AlterModelOptions(
            name="lot",
            options={"ordering": ["product__sku", "lot_number"]},
        ),
        migrations.AddConstraint(
            model_name="lot",
            constraint=models.UniqueConstraint(
                fields=("product", "lot_number"), name="unique_lot_per_product",
            ),
        ),
    ]
