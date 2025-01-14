create or replace function on_log_column_change()
returns trigger
as
    $$
    DECLARE
        column_name TEXT;
        unparsed_columns JSONB;
    BEGIN
        column_name := NEW.event || '_' || NEW.attribute;
        IF NEW.parse = TRUE THEN
            EXECUTE format(
                'ALTER TABLE $schema.logs ADD COLUMN IF NOT EXISTS %I JSONB GENERATED ALWAYS AS (parsed->%L) STORED',
                column_name,
                column_name
            );
        ELSE
            EXECUTE format(
                'ALTER TABLE $schema.logs DROP COLUMN IF EXISTS %I',
                column_name
            );
        END IF;
        RETURN NEW;
    END
$$
language plpgsql
;
DROP TRIGGER IF EXISTS log_columns_change ON $schema.log_columns;
CREATE TRIGGER log_column_change
AFTER UPDATE
ON $schema.log_columns
FOR EACH ROW EXECUTE PROCEDURE on_log_column_change();


create or replace function on_msg_column_change()
returns trigger
as
    $$
    DECLARE
        column_name TEXT;
        unparsed_columns JSONB;
    BEGIN
        column_name := NEW.attribute;
        IF NEW.parse = TRUE THEN
            EXECUTE format(
                'ALTER TABLE $schema.messages ADD COLUMN IF NOT EXISTS %I JSONB GENERATED ALWAYS AS (parsed->%L) STORED',
                column_name,
                column_name
            );
        ELSE
            EXECUTE format(
                'ALTER TABLE $schema.messages DROP COLUMN IF EXISTS %I',
                column_name
            );
        END IF;
        RETURN NEW;
    END
$$
language plpgsql
;
DROP TRIGGER IF EXISTS msg_columns_change ON $schema.msg_columns;
CREATE TRIGGER msg_columns_change
AFTER UPDATE
ON $schema.msg_columns
FOR EACH ROW EXECUTE PROCEDURE on_msg_column_change();
