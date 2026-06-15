import json

def main():
    try:
        with open("run_output.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error: {e}")
        return

    results = data.get("results", [])
    sessions = {}
    for r in results:
        sess = r.get("session")
        turn = r.get("turn")
        qid = r.get("qid")
        if sess not in sessions:
            sessions[sess] = []
        sessions[sess].append((turn, qid))

    print(f"Total sessions: {len(sessions)}")
    multi = 0
    for sess, turns in sessions.items():
        if len(turns) > 1:
            multi += 1
            print(f"Session {sess} has {len(turns)} turns: {turns}")
            
    print(f"Multi-turn sessions: {multi}")

if __name__ == "__main__":
    main()
