import sys


def multiply(a: float, b: float) -> float:
    return a * b


if __name__ == "__main__":
    print(multiply(float(sys.argv[1]), float(sys.argv[2])))
