import argparse
import glob
import json
import os.path
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from git import Repo

from optimum_benchmark import main


PATH_TO_REPO = Path(__file__).parent.parent.resolve()


@contextmanager
def checkout_commit(repo: Repo, commit_id: str):
    """
    Context manager that checks out a given commit when entered, but gets back to the reference it was at on exit.
    Args:
        repo (`git.Repo`): A git repository (for instance the Transformers repo).
        commit_id (`str`): The commit reference to checkout inside the context manager.
    """
    current_head = repo.head.commit if repo.head.is_detached else repo.head.ref

    try:
        repo.git.checkout(commit_id)
        yield

    finally:
        repo.git.checkout(current_head)


def summarize(run_dir, metrics, expand_metrics=False):
    """Produce a summary for each optimum-benchmark launched job's output directory found in `run_dir`.

    Each summary's format is as follows (for `expand_metrics=False`):
    ```
    {
        "model": "google/gemma-2b",
        "commit": "3cd6ed22e4d49219f300f5055e71e3929aba20d7",
        "config": "benchmark.input_shapes.batch_size=1,benchmark.input_shapes.sequence_length=5",
        "metrics": {
            "decode.latency.mean": 1.624666809082031,
            "per_token.latency.mean": 0.012843788806628804,
            "per_token.throughput.value": 77.85864553330948
        }
    }
    ```
    """
    reports = glob.glob(os.path.join(run_dir, "**/benchmark_report.json"), recursive=True)
    report_dirs = [str(Path(report).parent) for report in reports]

    summaries = []
    for report_dir in report_dirs:
        commit = re.search(r"/commit=(.+?)/", report_dir).groups()[0]

        # Ths looks like `benchmark.input_shapes.batch_size=1,benchmark.input_shapes.sequence_length=5`.
        # (we rely on usinng hydra's `--multirun` and `hydra.sweep.subdir=${hydra.job.override_dirname}`.
        report_dir_name = str(Path(report_dir).parts[-1])

        if not os.path.isfile(os.path.join(report_dir, "benchmark_report.json")):
            continue

        # Get the report
        report_file = os.path.join(report_dir, "benchmark_report.json")
        with open(report_file) as fp:
            report = json.load(fp)

        # Get the full experiment config
        config_file = os.path.join(report_dir, "experiment_config.json")
        with open(config_file) as fp:
            config = json.load(fp)
        model = config["backend"]["model"]

        # Deal with the model name containing `/` like `google/gemma-2b` as `/` create a subdirectory.
        if report_dir_name.split(",")[0] == model.split("/")[-1]:
            report_dir_name = ",".join(report_dir_name.split(",")[1:])

        metrics_values = {}
        # post-processing of report: show a few selected/important metric
        for metric in metrics:
            keys = metric.split(".")
            value = report
            current = metrics_values
            for key in keys:
                # Avoid KeyError when a user's specified metric has typo.
                # TODO: Give warnings.
                if key not in value:
                    continue
                value = value[key]

                if expand_metrics:
                    if isinstance(value, dict):
                        if key not in current:
                            current[key] = {}
                            current = current[key]
                    else:
                        current[key] = value

            if not expand_metrics:
                metrics_values[metric] = value

        # show some config information
        print(f"model: {model}")
        print(f"commit: {commit}")
        print(f"config: {report_dir_name}")
        if len(metrics_values) > 0:
            print("metrics:")
            if expand_metrics:
                print(metrics_values)
            else:
                for metric, value in metrics_values.items():
                    print(f"  - {metric}: {value}")
        print("-" * 80)

        summary = {
            "model": model,
            "commit": commit,
            "config": report_dir_name,
            "metrics": metrics_values,
        }
        summaries.append(summary)

        with open(os.path.join(report_dir, "summary.json"), "w") as fp:
            json.dump(summary, fp, indent=4)

    # TODO: upload to Hub
    return summaries


def combine_summaries(summaries):
    """Combine a list of summary obtained from the function `summarize`.

    The combined summary's format is as follows:
    ```
    "google/gemma-2b": {
        "benchmark.input_shapes.batch_size=1,benchmark.input_shapes.sequence_length=5": {
            "3cd6ed22e4d49219f300f5055e71e3929aba20d7": {
                "metrics": {"decode.latency.mean": 1.624666809082031}
            },
            "c97ee28b117c0abe8e08891f402065e4df6d72aa": {
                "metrics": {"decode.latency.mean": 1.6278163452148438}
            }
        },
        "benchmark.input_shapes.batch_size=2,benchmark.input_shapes.sequence_length=5": {
            "3cd6ed22e4d49219f300f5055e71e3929aba20d7": {
                "metrics": {"decode.latency.mean": 1.6947791748046876}
            },
            "c97ee28b117c0abe8e08891f402065e4df6d72aa": {
                "metrics": {
                    "decode.latency.mean": 1.6980519409179688}
            }
        }
    }
    ```
    """
    combined = {}
    for summary in summaries:
        model = summary["model"]
        config = summary["config"]
        commit = summary["commit"]

        if model not in combined:
            combined[model] = {}

        if config not in combined[model]:
            combined[model][config] = {}

        if commit not in combined[model][config]:
            combined[model][config][commit] = {"metrics": summary["metrics"]}

    with open(os.path.join(exp_run_dir, "summary.json"), "w") as fp:
        json.dump(combined, fp, indent=4)

    # TODO: upload to Hub
    print(json.dumps(combined, indent=4))

    return combined


if __name__ == "__main__":

    def list_str(values):
        return values.split(",")

    parser = argparse.ArgumentParser()

    parser.add_argument("--config-dir", type=str, required=True, help="The path to the config directory.")
    parser.add_argument("--config-name", type=str, required=True, help="The config name.")

    # arguments specific to this wrapper for our own customization
    parser.add_argument("--ensure_empty", type=bool, default=True, help="If to create a temporary directory.")
    parser.add_argument(
        "--commit",
        type=list_str,
        default="",
        help="Comma-separated list of branch names and/or commit sha values on which the benchmark will run.",
    )
    parser.add_argument("--metrics", type=str, help="The metrics to be included in the summary.")
    args, optimum_benchmark_args = parser.parse_known_args()

    repo = Repo(PATH_TO_REPO)

    metrics = ["decode.latency.mean"]
    if args.metrics is not None:
        metrics = args.metrics.split(",")

    # Get `backend.model` in a hacky way: We want to control the experiment flow manually.
    models = [""]
    for idx, arg in enumerate(optimum_benchmark_args):
        if arg.startswith("backend.model="):
            models = arg[len("backend.model=") :]
            models = models.split(",")
            break
    optimum_benchmark_args = [arg for arg in optimum_benchmark_args if not arg.startswith("backend.model=")]

    # Get the commit(s)
    current_head = str(repo.head.commit) if repo.head.is_detached else str(repo.head.ref)
    commits = [x for x in args.commit if x != ""]
    if len(commits) == 0:
        commits = [current_head]
    elif len(commits) == 1 and commits[0] == "diff":
        # compare to `main`
        commits = ["main", current_head]

    # Get the specified run directory
    run_dir_arg_idx, run_dir = -1, None
    sweep_dir_arg_idx, sweep_dir = -1, None
    for idx, arg in enumerate(optimum_benchmark_args):
        if arg.startswith("hydra.run.dir="):
            run_dir = arg[len("hydra.run.dir=") :]
            run_dir_arg_idx = idx
        elif arg.startswith("hydra.sweep.dir="):
            sweep_dir = arg[len("hydra.sweep.dir=") :]
            sweep_dir_arg_idx = idx
    exp_run_dir, arg_dix, arg_name = (
        (sweep_dir, sweep_dir_arg_idx, "hydra.sweep.dir")
        if "--multirun" in optimum_benchmark_args
        else (run_dir, run_dir_arg_idx, "hydra.run.dir")
    )

    # TODO: not hardcoded
    if exp_run_dir is None and args.ensure_empty:
        exp_run_dir = "_benchmark"

    if args.ensure_empty:
        os.makedirs(exp_run_dir, exist_ok=True)
        exp_run_dir = tempfile.mkdtemp(dir=exp_run_dir)

    run_summaries = []
    for commit in commits:
        with checkout_commit(repo, commit):
            commit = str(repo.head.commit)

            commit_run_dir = exp_run_dir
            if exp_run_dir is not None:
                commit_run_dir = os.path.join(exp_run_dir, rf"commit\={commit}")

            print(f"Run benchmark on commit: {commit}")

            for model in models:
                model_arg = [f"backend.model={model}"] if model != "" else []
                dir_args = []
                if commit_run_dir is not None:
                    if arg_dix > -1:
                        optimum_benchmark_args[arg_dix] = f"{arg_name}={commit_run_dir}"
                    else:
                        dir_args = [f"hydra.sweep.dir={commit_run_dir}", f"hydra.run.dir={commit_run_dir}"]
                main(args.config_dir, args.config_name, model_arg + dir_args + optimum_benchmark_args)

            if commit_run_dir is not None:
                # Need to remove the `\` character
                summaries = summarize(commit_run_dir.replace(r"commit\=", "commit="), metrics)
                run_summaries.extend(summaries)

    # aggregate the information across the commits
    if exp_run_dir is not None:
        with open(os.path.join(exp_run_dir, "summaries.json"), "w") as fp:
            json.dump(run_summaries, fp, indent=4)

        combined_summary = combine_summaries(run_summaries)
