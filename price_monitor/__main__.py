import sys


def main() -> int:
    if len(sys.argv) == 1:
        from price_monitor.gui import run_gui

        return run_gui()
    from price_monitor.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
