from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0007_lot_number"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="warehouse",
            options={"ordering": ["code"], "verbose_name": "almacén", "verbose_name_plural": "almacenes"},
        ),
        migrations.AlterModelOptions(
            name="location",
            options={
                "ordering": ["warehouse__code", "code"],
                "verbose_name": "ubicación", "verbose_name_plural": "ubicaciones",
            },
        ),
        migrations.AlterModelOptions(
            name="product",
            options={"ordering": ["sku"], "verbose_name": "producto", "verbose_name_plural": "productos"},
        ),
        migrations.AlterModelOptions(
            name="lot",
            options={
                "ordering": ["product__sku", "lot_number"],
                "verbose_name": "número de lote", "verbose_name_plural": "números de lote",
            },
        ),
        migrations.AlterModelOptions(
            name="stockitem",
            options={
                "ordering": ["product__sku", "location__code"],
                "verbose_name": "existencia", "verbose_name_plural": "existencias",
            },
        ),
        migrations.AlterModelOptions(
            name="stockmovement",
            options={
                "ordering": ["-created_at", "-id"],
                "verbose_name": "movimiento", "verbose_name_plural": "movimientos",
            },
        ),
        migrations.AlterModelOptions(
            name="reservation",
            options={
                "ordering": ["-created_at", "-id"],
                "verbose_name": "reserva", "verbose_name_plural": "reservas",
            },
        ),
        migrations.AlterField(
            model_name="warehouse",
            name="code",
            field=models.CharField(max_length=20, unique=True, verbose_name="código"),
        ),
        migrations.AlterField(
            model_name="location",
            name="barcode",
            field=models.CharField(
                blank=True, max_length=100, null=True, unique=True, verbose_name="código de barras",
            ),
        ),
        migrations.AlterField(
            model_name="product",
            name="barcode",
            field=models.CharField(
                blank=True, max_length=100, null=True, unique=True, verbose_name="código de barras",
            ),
        ),
        migrations.AlterField(
            model_name="product",
            name="sku",
            field=models.CharField(max_length=64, unique=True, verbose_name="SKU"),
        ),
    ]
