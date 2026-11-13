"""Provision shared slots; shrinking drains existing work without killing it."""

import argparse
import os

import psycopg


def configure(dsn, slots):
    if type(slots) is not int or not 1 <= slots <= 128:
        raise ValueError("capacity outside bounds")
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(72441002)")
        conn.execute(
            "UPDATE public.mcp_execution_slots SET draining=true WHERE slot_id>%s", (slots,)
        )
        conn.execute(
            "DELETE FROM public.mcp_execution_slots WHERE draining AND run_id IS NULL AND NOT quarantined"
        )
        conn.execute(
            "INSERT INTO public.mcp_execution_slots(slot_id) SELECT generate_series(1,%s) ON CONFLICT(slot_id) DO UPDATE SET draining=false",
            (slots,),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slots", required=True, type=int)
    args = parser.parse_args()
    configure(os.environ["DATABASE_URL"], args.slots)


if __name__ == "__main__":
    main()
