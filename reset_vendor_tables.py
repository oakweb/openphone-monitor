#!/usr/bin/env python3
"""
Reset vendor-related tables to ensure they're created properly
"""

from main import app, db
from models import Vendor, VendorJob, VendorInvoiceData, VendorComment

with app.app_context():
    print("Dropping and recreating vendor-related tables...")
    
    # Drop tables in reverse order of dependencies
    try:
        VendorComment.__table__.drop(db.engine)
        print("✓ Dropped vendor_comments table")
    except:
        print("- vendor_comments table doesn't exist")
    
    try:
        VendorInvoiceData.__table__.drop(db.engine)
        print("✓ Dropped vendor_invoice_data table")
    except:
        print("- vendor_invoice_data table doesn't exist")
    
    try:
        VendorJob.__table__.drop(db.engine)
        print("✓ Dropped vendor_jobs table")
    except:
        print("- vendor_jobs table doesn't exist")
    
    try:
        Vendor.__table__.drop(db.engine)
        print("✓ Dropped vendors table")
    except:
        print("- vendors table doesn't exist")
    
    # Recreate all tables
    db.create_all()
    print("\n✅ All tables recreated successfully!")
    
    # Verify columns
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    
    print("\nVendor table columns:")
    for column in inspector.get_columns('vendors'):
        print(f"  - {column['name']} ({column['type']})")
    
    print("\nVendorComment table columns:")
    for column in inspector.get_columns('vendor_comments'):
        print(f"  - {column['name']} ({column['type']})")