"""Final frozen provider smoke. Exit 2 means the 14/20 normalized coverage gate failed."""
from pricing_probe import main


if __name__ == "__main__":
    main(kind="final-smoke", require_coverage=True)
