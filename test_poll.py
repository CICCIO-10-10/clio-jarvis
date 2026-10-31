
def main() -> int:
    import requests, config, time

    res = requests.post(
        "http://localhost:5000/api/command",
        headers={"X-Clio-Key": config.CLIO_API_KEY},
        json={"text": "apri youtube"}
    )

    data = res.json()
    print("Post response:", data)
    task_id = data.get("task_id")

    if not task_id:
        print("❌ No task_id returned!")
    return 0

    print(f"Polling status for task {task_id}...")
    for _ in range(15):  # 30 seconds max
        time.sleep(2)
        poll = requests.get(f"http://localhost:5000/api/task_status?task_id={task_id}", headers={"X-Clio-Key": config.CLIO_API_KEY})
        pdata = poll.json()
        print("Poll:", pdata)
        if pdata.get("status") in ["completed", "error"]:
            print("\n✅ Final Result:")
            print(pdata)
            break


if __name__ == "__main__":
    raise SystemExit(main())
