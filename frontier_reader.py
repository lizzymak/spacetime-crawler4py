import shelve

db = shelve.open("frontier.shelve")

print("Keys:")
for key in db.keys():
    print(key, type(db[key]))

print("\nPreview:")
for key in db.keys():
    value = db[key]
    print("\nKEY:", key)
    print(str(value)[:2000])

db.close()
