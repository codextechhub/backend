from django.core.management.base import BaseCommand
import pymysql as sql
import sys

class Command(BaseCommand):
    help = 'Drop tables dynamically from the databases'
    
    def add_arguments(self, parser):
        parser.add_argument('db_name', type=str, help='Name of the database to drop tables from')
        
    def handle(self, *args, **options):
        db_name = options['db_name']
        
        conn = sql.connect(
            host='localhost',
            user='root',
            password='',
            database= db_name
            
        )
        cursor = conn.cursor()
        
        try:
        
            self.stdout.write("Do you want to drop all the tables in this database? y/n >>> ")
            drop_option = sys.stdin.readline().strip().lower()
            self.stdout.write("\n")
            
            if drop_option == 'y':
                cursor.execute("SET FOREIGN_KEY_CHECKS =  0")
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
                
                for table in tables:
                    table_name = table[0]
                    cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
                    
                cursor.execute("SET FOREIGN_KEY_CHECKS =  1")
                    
                self.stdout.write(self.style.SUCCESS(f"All Tables dropped successfully"))
                    
                conn.commit()
                
            else:
                self.stdout.write("Type in the table you want to drop>>> ")
                table_name = sys.stdin.readline().strip()
                self.stdout.write("\n")
                
                cursor.execute("SET FOREIGN_KEY_CHECKS =  0")
                cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
                cursor.execute("SET FOREIGN_KEY_CHECKS =  1")
                
                self.stdout.write(self.style.SUCCESS(f"Table `{table_name}` dropped successfully"))
                
                conn.commit()
                
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error: {e}"))
            
        finally:
            conn.close()
            self.stdout.write(self.style.SUCCESS("All operations completed."))
                
                
                
        
        

        