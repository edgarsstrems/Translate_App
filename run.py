if __name__ == "__main__":
    import sys
    import faulthandler
    from pathlib import Path

    diagnostic = None
    if "--smoke-test" in sys.argv:
        report = Path(sys.argv[sys.argv.index("--smoke-test") + 1]).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        diagnostic = report.with_suffix(".startup.log").open("w", encoding="utf-8")
        sys.stderr = diagnostic
        faulthandler.enable(file=diagnostic)
        faulthandler.dump_traceback_later(20, file=diagnostic)
    try:
        from church_translator.main import main
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        if diagnostic:
            faulthandler.cancel_dump_traceback_later()
            diagnostic.close()
