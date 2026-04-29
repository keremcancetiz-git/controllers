def log_sample(
    experiment_folder: str,
    log_entry: str
    ):

    log_file = experiment_folder + "\\" + "testbench_log.log"

    if not log_entry.endswith("\n"):
        log_entry += "\n"

    # Open the file in append mode and write
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_entry)