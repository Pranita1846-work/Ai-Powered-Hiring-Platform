#!/bin/bash
# Quick deployment script for Hostinger
# Upload this file and run: bash deploy.sh

echo "======================================"
echo "  Hiring Platform - Hostinger Deploy"
echo "======================================"
echo ""

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Create uploads directory
echo "Creating uploads directory..."
mkdir -p static/uploads
chmod 755 static/uploads

# Set permissions
echo "Setting file permissions..."
find . -type f -exec chmod 644 {} \;
find . -type d -exec chmod 755 {} \;
chmod 755 passenger_wsgi.py

echo ""
echo "======================================"
echo "  Deployment Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Create .env file with your credentials"
echo "2. Restart Python application in hPanel"
echo "3. Visit your domain to test"
echo ""
