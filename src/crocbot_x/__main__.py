import argparse

def main():
    p = argparse.ArgumentParser(prog="crocbot-x")
    p.add_argument("--dry-run", action="store_true", default=True)
    args = p.parse_args()
    print("Crocbot X 0.1.0 — dry-run, pay disabled, $0 moved.")

if __name__ == "__main__":
    main()
