@echo off
REM Database Setup Script for Hiring Platform
echo Setting up PostgreSQL database...

REM Add PostgreSQL to PATH
set PATH=%PATH%;C:\Program Files\PostgreSQL\17\bin

REM Create database
echo Creating database 'hiring_platform'...
psql -U postgres -c "CREATE DATABASE hiring_platform;"

IF %ERRORLEVEL% EQU 0 (
    echo Database created successfully!
    echo.
    echo Now initializing tables...
    python init_database.py
) ELSE (
    echo Failed to create database. Please check your password.
    echo You can also create it manually using pgAdmin.
)

pause
