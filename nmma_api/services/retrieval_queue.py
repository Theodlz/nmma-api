import time
from datetime import datetime

from nmma_api.tools.expanse import retrieve
from nmma_api.tools.webhook import upload_analysis_results
from nmma_api.utils.config import load_config
from nmma_api.utils.logs import make_log
from nmma_api.utils.mongo import Mongo

log = make_log("retrieval_queue")

config = load_config()

mongo = Mongo(**config["database"])
retrieval_wait_time = config["wait_times"]["retrieval"]
max_upload_failures = config["wait_times"].get("max_upload_failures", 10)


def retrieval_queue():
    """Retrieve analysis results from expanse."""
    while True:
        try:
            # get the analysis requests that have been processed
            analysis_requests = mongo.db.analysis.find(
                {
                    "status": {
                        "$in": [
                            "running",
                            "retry_upload",
                            "failed_submission_to_upload",
                        ]
                    }
                }
            )
            analysis_requests = [x for x in analysis_requests]
            log(
                f"Found {len(analysis_requests)} analysis requests to retrieve/process."
            )
            for analysis in analysis_requests:
                if (
                    datetime.strptime(analysis["invalid_after"], "%Y-%m-%d %H:%M:%S.%f")
                    < datetime.utcnow()
                ):
                    log(
                        f"Analysis {analysis['_id']} webhook has expired. Skipping and deleting the results if they exist."
                    )
                    mongo.db.analysis.update_one(
                        {"_id": analysis["_id"]},
                        {"$set": {"status": "expired"}},
                    )
                    try:
                        mongo.db.results.delete_one({"analysis_id": analysis["_id"]})
                    except Exception:
                        pass
                    continue

                if analysis["status"] == "failed_submission_to_upload":
                    log(
                        f"Analysis {analysis['_id']} failed to submit to expanse. Updating status upstream."
                    )
                    results = {
                        "status": "failure",
                        "message": analysis.get("error", "unknown error"),
                    }
                    upload_analysis_results(
                        results, analysis
                    )  # for a failure, we don't bother with retries
                    mongo.db.analysis.update_one(
                        {"_id": analysis["_id"]},
                        {"$set": {"status": "failed_submission"}},
                    )
                    continue

                if (
                    analysis["status"] == "retry_upload"
                    and analysis.get("nb_upload_failures", 0) >= max_upload_failures
                ):
                    log(
                        f"Analysis {analysis['_id']} has failed to upload 10 times. Skipping and deleting the results."
                    )
                    mongo.db.analysis.update_one(
                        {"_id": analysis["_id"]},
                        {"$set": {"status": "failed_upload"}},
                    )
                    mongo.db.results.delete_one({"analysis_id": analysis["_id"]})
                    continue

                if analysis["status"] == "running":
                    results = retrieve(analysis)
                else:
                    try:
                        results = mongo.db.results.find_one(
                            {"analysis_id": analysis["_id"]}
                        )["results"]
                    except Exception:
                        results = retrieve(analysis)

                if results is not None:
                    log(
                        f"Uploading results to webhook for analysis {analysis['_id']} ({analysis['resource_id']}, {analysis['created_at']})"
                    )

                    if analysis["status"] == "running":
                        mongo.insert_one(
                            "results",
                            {"analysis_id": analysis["_id"], "results": results},
                        )

                    uploaded, error = upload_analysis_results(results, analysis)
                    if uploaded:
                        mongo.db.analysis.update_one(
                            {"_id": analysis["_id"]},
                            {"$set": {"status": "completed"}},
                        )
                        # delete the results from the database
                        mongo.db.results.delete_one({"analysis_id": analysis["_id"]})
                    else:
                        mongo.db.analysis.update_one(
                            {"_id": analysis["_id"]},
                            {
                                "$set": {
                                    "status": "retry_upload",
                                    "nb_upload_failures": analysis.get(
                                        "nb_upload_failures", 0
                                    )
                                    + 1,
                                    "upload_error": error,
                                }
                            },
                        )
                else:
                    log(f"Analysis {analysis['_id']} has not completed yet. Skipping.")
        except Exception as e:
            log(f"Failed to retrieve analysis results from expanse: {e}")

        time.sleep(retrieval_wait_time)


if __name__ == "__main__":
    retrieval_queue()
