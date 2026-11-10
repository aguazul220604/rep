import os

import dataiku

import bundles_scan


def main():
    server = bundles_scan.detect_current_server()
    folder = dataiku.Folder(bundles_scan.S3_FOLDER_NAME)
    budget = bundles_scan.ScanBudget(
        max_calls=int(os.environ.get("BUNDLE_INVENTORY_MAX_CALLS", "5000")),
        time_budget_s=float(
            os.environ.get("BUNDLE_INVENTORY_TIME_BUDGET_S", "3600")
        ),
    )

    rows, warnings = bundles_scan.build_inventory(folder, server, budget=budget)
    total = bundles_scan.save_inventory(rows, bundles_scan.INVENTORY_DATASET_NAME)

    print(
        f"[build_bundles_inventory] host={server.get('hostname')} "
        f"flow={server.get('flow')} env={server.get('environment')} "
        f"rows={total} detail_calls={budget.calls}/{budget.max_calls}"
    )
    for warning in warnings:
        print(f"[build_bundles_inventory] {warning}")


if __name__ == "__main__":
    main()
