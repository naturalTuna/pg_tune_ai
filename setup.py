from setuptools import setup

setup(
    name="pgtune",
    version="0.1",
    py_modules=["pgtune"],  # 실행할 파이썬 파일 이름 (확장자 제외)
    entry_points={
        "console_scripts": [
            "pgtune=pgtune:main",  # 터미널 명령어=파일명:시작할함수명
        ],
    },
)
