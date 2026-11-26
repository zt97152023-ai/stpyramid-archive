"""Command-line entry point retained for existing image archive workflows."""
from pyramid_image_archive import encode_native_pyramid as build
from pyramid_image_archive import main

__all__ = ["build", "main"]


if __name__ == "__main__":
    main()
