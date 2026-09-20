from glob import glob

from setuptools import setup

package_name = 'field_calib'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    description='Overhead camera extrinsic calibration from the table edges',
    license='TODO',
    entry_points={
        'console_scripts': [
            'field_calib_node = field_calib.node:main',
        ],
    },
)
