from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields.ranges import DateTimeRangeField
from django.db import connection, models
from django.db.models.signals import post_migrate, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import camel_case_to_spaces


def default_range():
    """Return the default tstzrange."""
    return f"[{timezone.now()},)"


def generate_bitemporal_tables(
    mixin_cls: type[models.Model],
    key_fields_and_equality_operators: list[tuple[str, str]],
) -> tuple[type[models.Model], type[models.Model]]:
    """Generate current and history models inheriting from ``mixin_cls``."""
    class_name: str = mixin_cls.__name__
    if not class_name.endswith("Base"):
        raise ValueError(
            "Abstract class name should end with `Base` to avoid confusion"
        )

    # trim "Base"
    class_name = class_name[:-4]
    table_name = camel_case_to_spaces(class_name.lower())

    class CurrentMetaClass(models.base.ModelBase):
        def __new__(cls, name, bases, attrs):
            return super().__new__(cls, class_name, bases, attrs)

    class HistoryMetaClass(models.base.ModelBase):
        def __new__(cls, name, bases, attrs):
            return super().__new__(cls, f"{class_name}History", bases, attrs)

    class Current(mixin_cls, metaclass=CurrentMetaClass):
        """Mixin for current table, i.e. values that have not been expired.

        Django cannot set a database-side default for these ranges.
        """

        # Primary key, because the bitemporal key is not unique irrespective of time.
        # UUID is acceptable too. An incrementing integer is a stylistic choice.
        row_id = models.BigAutoField(primary_key=True)

        # time dimension relevant to the application
        app_period = DateTimeRangeField(null=False, default=default_range)

        # time dimension relevant to the "transaction" / persistence layer
        txn_period = DateTimeRangeField(null=False, default=default_range)

        class Meta:
            app_label = getattr(mixin_cls, "_meta").app_label
            db_table = f"{app_label}_{table_name}"

            constraints = [
                models.UniqueConstraint(
                    fields=(
                        "app_period",
                        *(
                            key_field
                            for key_field, _ in key_fields_and_equality_operators
                        ),
                    ),
                    # TODO: more django-idiomatic name convention
                    name=f"{db_table}_bitemporal_unique_key",
                ),
                ExclusionConstraint(
                    expressions=[
                        *(
                            (key_field, op)
                            for key_field, op in key_fields_and_equality_operators
                        ),
                        ("app_period", "&&"),
                    ],
                    # TODO: more django-idiomatic name convention
                    name=f"{db_table}_bitemporal_key_exclusion",
                ),
            ]

            # TODO indexes

    class History(mixin_cls, metaclass=HistoryMetaClass):
        """Base for 'history' table, i.e. values that have been expired."""

        # Primary key, because the bitemporal key is not unique irrespective of time.
        # UUID is acceptable too. An incrementing integer is a stylistic choice.
        row_id = models.BigAutoField(primary_key=True)

        # time dimension relevant to the application
        app_period = DateTimeRangeField(null=False, default=default_range)

        # the time dimension relevant to the "transaction" i.e. persistence in `current`
        # `txn_period` joins the primary key: history repeats `row_id`
        # across corrections, or the table would not be a useful history.
        txn_period = DateTimeRangeField(null=False, default=default_range)

        class Meta:
            app_label = getattr(mixin_cls, "_meta").app_label
            db_table = f"{app_label}_{table_name}_history"

            constraints = [
                # No unique constraint: history can repeat a key and `app_period`.
                # rows with identical bitemporal-key values can overlap on `app_period`
                ExclusionConstraint(
                    expressions=[
                        *(
                            (key_field, op)
                            for key_field, op in key_fields_and_equality_operators
                        ),
                        ("app_period", "&&"),
                    ],
                    name=f"{db_table}_bitemporal_key_exclusion",
                ),
                models.UniqueConstraint(
                    fields=("row_id", "txn_period"),
                    name=f"{db_table}_composite_key",
                ),
            ]

    @receiver(signal=[pre_save, pre_delete], sender=History)
    def history_mutation_preventer(sender, **kwargs):
        raise ValueError(f"May not mutate instances of {sender.__class__}")

    @receiver(signal=[post_migrate], sender=History)
    def history_trigger_emitter(sender, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TRIGGER versioning_trigger "
                f'BEFORE INSERT OR DELETE OR UPDATE ON "{sender._meta.db_table}" '
                "FOR EACH ROW EXECUTE PROCEDURE "
                f"public.record_history('{sender._meta.db_table}')"
            )

    return Current, History
